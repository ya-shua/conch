"""一键起全家桶：外部服务 + 海螺本体，窗口全隐藏，最后只打开海螺控制台。

桌面「海螺启动」调这个（经 start_conch.vbs）。全程无窗口：
子进程用 CREATE_NO_WINDOW + STARTF_USESHOWWINDOW(SW_HIDE)，实测控制台不会露出来。
唯一会出现的窗口就是最后打开的那个浏览器页面。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # 海螺/
PROJECT = ROOT.parent                                   # 项目根目录的上一级
CONFIG = ROOT / "config.json"
PYTHONW = ROOT / ".venv" / "Scripts" / "pythonw.exe"
NO_WINDOW = 0x08000000


def _startupinfo() -> subprocess.STARTUPINFO:
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0  # SW_HIDE
    return info


def log(message: str) -> None:
    try:
        path = ROOT / "data" / "logs" / "start.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} {message}\n")
    except OSError:
        pass


def cfg() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8-sig"))


def panel_url() -> str:
    panel = cfg().get("panel") or {}
    token = str(panel.get("token") or "")
    port = int(panel.get("port") or 7801)
    return f"http://127.0.0.1:{port}/" + (f"?token={token}" if token else "")


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.4):
            return True
    except OSError:
        return False


def host_up(url: str) -> bool:
    try:
        req = urllib.request.Request(url.split("?")[0] + "api/status")
        req.add_header("x-console-token", str((cfg().get("panel") or {}).get("token") or ""))
        urllib.request.urlopen(req, timeout=1.5)
        return True
    except Exception:
        return False


def spawn(argv: list[str], cwd: str, log_name: str, env_extra: dict | None = None) -> int:
    logs = ROOT / "data" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    handle = open(logs / log_name, "ab")
    env = dict(os.environ)
    if env_extra:
        env.update({str(k): str(v) for k, v in env_extra.items()})
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=NO_WINDOW,
        startupinfo=_startupinfo(),
    )
    return proc.pid


def _already_running(item: dict) -> bool:
    """没有端口的服务，靠命令行认一下，别重复起。"""
    args = [str(a) for a in (item.get("args") or [])]
    if not args:
        return False
    needle = args[-1].replace("\\", "\\\\")
    cmd = (
        "Get-CimInstance Win32_Process -Filter \"Name='node.exe' OR Name='python.exe' OR Name='pythonw.exe'\" | "
        f"Where-Object {{ $_.CommandLine -match '{needle}' }} | "
        "Select-Object -First 1 -ExpandProperty ProcessId"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True, text=True, timeout=20,
            creationflags=NO_WINDOW, startupinfo=_startupinfo(),
        ).stdout.strip()
        return bool(out)
    except Exception:
        return False


def _launch_items() -> list[dict]:
    """launchers.json + 各插件 metadata.json 里的 launch 段（手写的优先）。"""
    import sys

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        from core import launchers as L

        data = cfg()
        roots = [str(ROOT / "plugins")]
        for extra in data.get("plugin_paths") or []:
            roots.append(str(extra))
        return L.load(ROOT / "launchers.json", plugin_roots=roots)
    except Exception as exc:  # 兜底：至少把 launchers.json 里的起来
        log(f"  读启动清单出错（{exc}），只用 launchers.json")
        path = ROOT / "launchers.json"
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8-sig"))


def start_services() -> tuple[list[str], list[str]]:
    items = _launch_items()
    if not items:
        return [], []
    started, failed = [], []
    for item in items:
        name = item.get("name") or item.get("id")
        port = item.get("port")
        if port and port_open(int(port)):
            started.append(f"{name}（本来就在跑）")
            continue
        if not port and _already_running(item):
            started.append(f"{name}（本来就在跑）")
            continue
        cwd = str(item.get("cwd") or "")
        cmd = str(item.get("cmd") or "")
        if not cwd or not cmd or not Path(cwd).exists():
            failed.append(f"{name}（路径不对：{cwd}）")
            continue
        argv = [cmd, *[str(a) for a in (item.get("args") or [])]]
        try:
            pid = spawn(argv, cwd, f"{item.get('id')}.log", item.get("env"))
        except Exception as exc:
            failed.append(f"{name}（{exc}）")
            continue
        if port:
            ok = False
            for _ in range(75):
                time.sleep(0.4)
                if port_open(int(port)):
                    ok = True
                    break
            (started if ok else failed).append(
                f"{name}（端口 {port}）" if ok else f"{name}（30 秒没等到端口 {port}，pid {pid}）"
            )
        else:
            started.append(f"{name}（pid {pid}）")
    return started, failed


def remember_url(url: str) -> None:
    try:
        target = ROOT / "data" / "panel.url"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(url, encoding="utf-8")
    except OSError as exc:
        log(f"  写 panel.url 失败：{exc}")


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    import ctypes

    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    return False


def take_lock() -> bool:
    lock = ROOT / "data" / "start.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        raw = lock.read_text(encoding="utf-8").strip()
        pid = int(raw) if raw.isdigit() else 0
        if not _alive(pid) or time.time() - lock.stat().st_mtime > 120:
            lock.unlink(missing_ok=True)
    except (OSError, ValueError):
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def release_lock() -> None:
    (ROOT / "data" / "start.lock").unlink(missing_ok=True)


def main() -> int:
    url = panel_url()

    if not take_lock():
        # 已经有一轮在起了：等它把面板拉起来就行
        for _ in range(60):
            time.sleep(0.5)
            if host_up(url):
                break
        return 0
    try:
        return _main_locked(url)
    finally:
        release_lock()


def _guard_running() -> bool:
    cmd = (
        "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | "
        "Where-Object { $_.CommandLine -match 'window_guard' } | "
        "Select-Object -First 1 -ExpandProperty ProcessId"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True, text=True, timeout=20,
            creationflags=NO_WINDOW, startupinfo=_startupinfo(),
        ).stdout.strip()
        return bool(out)
    except Exception:
        return False


def _main_locked(url: str) -> int:
    remember_url(url)

    # 先起海螺本体：面板页面第一个可用，启动脚本就能立刻把它打开；
    # 其它服务在后台继续慢慢起，不再挡着页面。
    if not host_up(url):
        pid = spawn([str(PYTHONW), str(ROOT / "main.py"), "--config", str(CONFIG)],
                    str(ROOT), "conch-run.log")
        log(f"海螺本体 pid {pid}")

    # 守卫要在服务起来之前就位（查一次进程要 1 秒多，所以放在本体之后）
    if not _guard_running():
        spawn([str(PYTHONW), str(ROOT / "scripts" / "window_guard.py")], str(ROOT), "window_guard_supervisor.log")
        log("窗口守卫已起")

    started, failed = start_services()
    for line in started:
        log("起 " + line)

    if not host_up(url):
        failed.append("海螺本体没起来（看 data\\logs\\conch-run.log）")

    if not port_open(3001):
        failed.append("SnowLuma 还没登录 QQ（打开 127.0.0.1:5099 扫码即可）")

    for line in failed:
        log("没 " + line)

    # 页面由 start_conch.vbs 负责打开：pythonw 这个无窗口进程开不出可见浏览器窗口
    log("  （页面由启动脚本打开）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())