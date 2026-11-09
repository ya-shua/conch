"""面板窗口认错进程 / 点了没反应 —— 两件事的回归测试。

真实故障（2026-11-09）：
  微光域和海螺一起跑的时候，点海螺弹出**微光域的浏览器**；
  或者干脆什么都不弹，日志里刷「面板进程还活着，但窗口还没出来」。

两个都出在同一个地方：只拿 pid 当身份。
  · Windows 会回收 pid，微光域那套 Playwright 的 msedge 随时拿到同一个号
  · 浏览器被点 X 关掉、进程还在时，旧代码返回 waiting 干等 —— 那条路永远是死路

所以这里把「进程身份」当成契约来测：
  pid 一样但启动时间不一样 → 不是同一个进程
  进程在、窗口没了 → 再开一个，不许 waiting
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import panel_window as pw  # noqa: E402


class FakeProc:
    """一张 pid → (名字, 启动时间) 的表，冒充系统。"""

    def __init__(self):
        self.table: dict[int, tuple[str, float]] = {}
        self.windows: dict[int, list[int]] = {}
        self.alive_hwnds: dict[int, bool] = {}

    def add(self, pid: int, name: str = "msedge.exe", start: float = 1000.0, wins=()):
        self.table[pid] = (name, start)
        self.windows[pid] = list(wins)
        for h in wins:
            self.alive_hwnds[h] = True

    def install(self, monkeypatch):
        def _name(pid):
            return self.table.get(pid, ("", 0.0))[0] if pid in self.table else ""

        def _start(pid):
            return self.table.get(pid, ("", 0.0))[1] if pid in self.table else 0.0

        def _alive(pid):
            return pid in self.table

        monkeypatch.setattr(pw, "_proc_name", _name)
        monkeypatch.setattr(pw, "_proc_start", _start)
        monkeypatch.setattr(pw, "_process_alive", _alive)
        monkeypatch.setattr(pw, "_windows_of_pid", lambda pid: list(self.windows.get(pid, [])))
        monkeypatch.setattr(pw, "alive", lambda h: bool(h) and self.alive_hwnds.get(h, False))
        monkeypatch.setattr(pw, "raise_window", lambda h: bool(self.alive_hwnds.get(h)))
        monkeypatch.setattr(pw, "_owner_pid", lambda h: next(
            (pid for pid, wins in self.windows.items() if h in wins), 0))
        return self


@pytest.fixture()
def sysdir(tmp_path, monkeypatch):
    """把落盘的状态文件指到临时目录，别碰真机上的 data/。"""
    monkeypatch.setattr(pw, "STATE", tmp_path / "panel.hwnd")
    monkeypatch.setattr(pw, "PIDFILE", tmp_path / "panel.proc")
    monkeypatch.setattr(pw, "OPENING", tmp_path / "panel.opening")
    return tmp_path


def test_same_pid_different_start_is_not_same_process(sysdir, monkeypatch):
    """核心 bug：pid 被回收给了微光域的浏览器，不能认成我们的。"""
    fake = FakeProc().install(monkeypatch)
    fake.add(4321, start=1000.0, wins=[101])     # 我们的（已经不在了）
    pw._remember(101, 4321)

    # pid 复用：同一个号，启动时间变了 → 是微光域的进程
    fake.table[4321] = ("msedge.exe", 5000.0)
    fake.windows[4321] = [202]
    fake.alive_hwnds[101] = False                # 老窗口早没了

    win = pw._remembered_window()
    assert pw._window_matches(win) is False
    assert pw._same_proc(4321, 1000.0) is False


def test_window_match_requires_owner(sysdir, monkeypatch):
    fake = FakeProc().install(monkeypatch)
    fake.add(77, start=1000.0, wins=[9])
    pw._remember(9, 77)
    assert pw._window_matches(pw._remembered_window()) is True


def test_proc_state_roundtrip(sysdir, monkeypatch):
    FakeProc().install(monkeypatch)
    monkeypatch.setattr(pw, "_proc_start", lambda pid: 1234.5 if pid == 55 else 0.0)
    pw._remember_pid(55)
    assert pw._remembered_pid() == 55
    assert pw._remembered_proc()["start"] == pytest.approx(1234.5)


def test_legacy_pid_file_still_loads(sysdir, monkeypatch):
    """老版本只写了一个数字：能读出来（启动时间未知），别直接崩。"""
    FakeProc().install(monkeypatch)
    sysdir.mkdir(parents=True, exist_ok=True)
    (sysdir / "panel.proc").write_text("1234", encoding="ascii")
    (sysdir / "panel.hwnd").write_text("5678", encoding="ascii")
    assert pw._remembered_pid() == 1234
    assert pw._remembered_proc()["start"] == 0.0
    # hwnd 老格式没有属主信息，不敢认 —— 当没有，走重新开那条路
    assert pw._remembered_window() == {}


def test_window_closed_relaunches_instead_of_waiting(sysdir, monkeypatch):
    """进程还在、窗口被关：必须再开一个，不能返回 waiting。"""
    fake = FakeProc().install(monkeypatch)
    fake.add(888, start=1000.0, wins=[])
    pw._remember_pid(888)

    calls = {"spawned": 0}

    def fake_popen(argv, **kw):
        calls["spawned"] += 1
        # 模拟浏览器把窗口开出来
        fake.add(888, start=1000.0, wins=[555])

        class P:
            pid = 888

        return P()

    monkeypatch.setattr(pw.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(pw, "_browser_exe", lambda: r"C:\fake\msedge.exe")
    monkeypatch.setattr(pw, "PROFILE", sysdir / "profile")

    out = pw.open_panel("http://127.0.0.1:7801/", wait=3.0, log=lambda *a: None)
    assert out == "opened"
    assert calls["spawned"] == 1


def test_adopt_window_on_old_instance(sysdir, monkeypatch):
    """Chromium 把新窗口交给同配置的旧实例：spawn 的进程没窗口，窗口其实开着。

    旧代码到这一步只会干等 12 秒然后报 failed（玩家看到的就是「点了没反应」）。
    现在按「谁在用面板专用配置目录」把它认出来。
    """
    fake = FakeProc().install(monkeypatch)
    fake.add(36692, start=900.0, wins=[777])          # 老实例，窗口在它身上 # privacy-ok: 假 PID
    pw._remember_pid(36692)  # privacy-ok: 假 PID
    monkeypatch.setattr(pw, "_panel_profile_pids", lambda: [36692])  # privacy-ok: 假 PID

    out = pw.open_panel("http://127.0.0.1:7801/", wait=2.0, log=lambda *a: None)
    assert out == "raised"
    assert pw._remembered_window()["hwnd"] == 777


def test_raise_existing_window_without_launching(sysdir, monkeypatch):
    fake = FakeProc().install(monkeypatch)
    fake.add(999, start=1000.0, wins=[42])
    pw._remember(42, 999)

    def boom(*a, **kw):
        raise AssertionError("已经有窗口了，不许再开浏览器")

    monkeypatch.setattr(pw.subprocess, "Popen", boom)
    out = pw.open_panel("http://127.0.0.1:7801/", wait=3.0, log=lambda *a: None)
    assert out == "raised"
