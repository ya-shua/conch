"""launchers 的启停账本：PID 档案、三重查找、认脸再杀。不起真进程，全靠桩。"""

import json

import pytest

from core import launchers


@pytest.fixture(autouse=True)
def clean_state(tmp_path):
    launchers._PIDS.clear()
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    launchers.load(str(cfg))
    yield cfg
    launchers._PIDS.clear()


def _patch_kill(monkeypatch, image="node.exe", alive=None):
    """把杀进程那套外部调用全换成桩；alive 是个可变 dict，taskkill 后翻成 False。"""
    state = {"alive": True} if alive is None else alive
    calls = []

    def fake_run(argv, **kwargs):
        if argv and argv[0] == "taskkill":
            calls.append(list(argv))
            state["alive"] = False
        class _R:
            stdout = ""
        return _R()

    monkeypatch.setattr(launchers.subprocess, "run", fake_run)
    monkeypatch.setattr(launchers, "_pid_alive", lambda pid: state["alive"])
    monkeypatch.setattr(launchers, "_image_of", lambda pid: image)
    monkeypatch.setattr(launchers, "port_open", lambda port, **k: False)
    return calls


def test_pid_file_roundtrip(clean_state):
    launchers.record_pid("ring", 4321)
    saved = json.loads(
        (clean_state.parent / "data" / "launcher-pids.json").read_text(encoding="utf-8")
    )
    assert saved == {"ring": 4321}
    launchers._forget_pid("ring")
    assert launchers._read_pids() == {}


def test_record_pid_creates_missing_data_dir(clean_state):
    assert not (clean_state.parent / "data").exists()
    launchers.record_pid("video", 7)
    assert launchers._read_pids()["video"] == 7


def test_stop_reports_when_nothing_to_kill(clean_state, monkeypatch):
    monkeypatch.setattr(launchers, "_pid_on_port", lambda port: 0)
    result = launchers.stop({"id": "ring", "port": 5173})
    assert result["ok"] is False
    assert "没在跑" in result["error"]


def test_stop_refuses_to_kill_foreign_image(clean_state, monkeypatch):
    launchers.record_pid("ring", 999)
    calls = _patch_kill(monkeypatch, image="explorer.exe")
    result = launchers.stop({"id": "ring", "port": None})
    assert result["ok"] is False
    assert "explorer.exe" in result["error"]
    assert calls == []  # 认脸不过就不许动手


def test_stop_uses_pid_file_from_other_process(clean_state, monkeypatch):
    """核心场景：桌面启动器起的服务，控制台这边光凭档案里的 PID 也能关掉。"""
    launchers._write_pids(clean_state, {"ring": 4321})
    launchers._PIDS.clear()
    calls = _patch_kill(monkeypatch)
    result = launchers.stop({"id": "ring", "port": None})
    assert result["ok"] is True and result["pid"] == 4321
    assert any(argv[:1] == ["taskkill"] and "/T" in argv and "/F" in argv for argv in calls)
    assert launchers._read_pids() == {} and "ring" not in launchers._PIDS


def test_stop_falls_back_to_port(clean_state, monkeypatch):
    monkeypatch.setattr(launchers, "_pid_on_port", lambda port: 555)
    calls = _patch_kill(monkeypatch, image="python.exe")
    result = launchers.stop({"id": "ring", "port": 5173})
    assert result["ok"] is True and result["pid"] == 555
    assert calls


def test_stop_all_summarizes(clean_state, monkeypatch):
    monkeypatch.setattr(launchers, "stop", lambda item: {"ok": item["id"] == "ring"})
    result = launchers.stop_all([{"id": "ring"}, {"id": "video"}])
    assert result["stopped"] == ["ring"]
    assert result["skipped"] == ["video"]
