"""launchers 的启停账本：PID 档案、三重查找、认脸再杀。不起真进程，全靠桩。"""

import json
from pathlib import Path

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


def _plugin_dir(root, name, meta):
    d = root / name
    d.mkdir()
    (d / "metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )
    return d


def test_metadata_runtime_node_builds_command(clean_state, tmp_path, monkeypatch):
    """metadata 只写 runtime + entry，命令由海螺拼 —— 挪目录不用改路径。"""
    monkeypatch.setattr(launchers, "_node_bin", lambda cfg: r"C:\node\node.exe")
    d = _plugin_dir(tmp_path, "eyes", {
        "id": "eyes",
        "name": "留影",
        "launch": {"runtime": "node", "entry": "src/panel.mjs", "args": ["--port", "5188"], "port": 5188},
    })
    items = launchers.load(str(clean_state), [str(tmp_path)])
    item = next(i for i in items if i["id"] == "eyes")
    assert item["cmd"] == r"C:\node\node.exe"
    assert item["cwd"] == str(d)
    assert item["args"] == [str(Path("src/panel.mjs")), "--port", "5188"]


def test_metadata_runtime_python_prefers_plugin_venv(clean_state, tmp_path):
    d = _plugin_dir(tmp_path, "voice", {
        "id": "voice",
        "launch": {"runtime": "python", "entry": "panel/launch.py"},
    })
    bin_dir = d / ".venv" / "Scripts"
    bin_dir.mkdir(parents=True)
    (bin_dir / "pythonw.exe").write_text("", encoding="utf-8")
    items = launchers.load(str(clean_state), [str(tmp_path)])
    item = next(i for i in items if i["id"] == "voice")
    assert item["cmd"].endswith(str(Path(".venv/Scripts/pythonw.exe")))
    assert item["args"] == [str(Path("panel/launch.py"))]


def test_hand_written_entry_wins_but_metadata_fills_gaps(clean_state, tmp_path):
    """launchers.json 里手写的优先；它没写的字段由插件 metadata 补上。"""
    _plugin_dir(tmp_path, "ring", {
        "id": "ring",
        "launch": {"runtime": "node", "entry": "src/web/server.js", "port": 5173},
    })
    launchers._path(clean_state).write_text(
        json.dumps([{"id": "ring", "name": "环形的心", "port": 5200}], ensure_ascii=False),
        encoding="utf-8",
    )
    items = launchers.load(str(clean_state), [str(tmp_path)])
    item = next(i for i in items if i["id"] == "ring")
    assert item["port"] == 5200  # 手写的没被覆盖
    assert item["cmd"]  # 手写没写 cmd，从 metadata 补
    assert item["cwd"].endswith("ring")
