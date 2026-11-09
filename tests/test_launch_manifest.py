"""启动清单：给 launchers.json 路径也要能补出 cwd/cmd（一键启动那条路）。

背景：scripts/start_all.py 一直拿 `launchers.json` 调 `load()`，
`_host_config` 却按 config.json 的样子去 `.get("look")`，读到 list 直接抛
`'list' object has no attribute 'get'` —— 整份清单读不出来，一键启动只把
「路径不对」写进 start.log，环形的心/娱乐/微光域/留影 都起不来。
"""

import json
from pathlib import Path

from core import launchers


def _make_world(tmp_path: Path) -> tuple[Path, Path]:
    host = tmp_path / "host"
    (host / "plugins").mkdir(parents=True)
    (host / ".venv" / "Scripts").mkdir(parents=True)
    host_py = host / ".venv" / "Scripts" / "pythonw.exe"
    host_py.write_text("", encoding="utf-8")

    app = tmp_path / "an-app"
    app.mkdir()
    (app / "metadata.json").write_text(
        json.dumps({
            "id": "demo",
            "name": "示例应用",
            "launch": {"runtime": "python", "entry": "main.py", "port": 1234,
                       "url": "http://127.0.0.1:1234/"},
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    host.joinpath("config.json").write_text(
        json.dumps({"look": {"node": ""}, "plugin_paths": [str(app)]}, ensure_ascii=False),
        encoding="utf-8",
    )
    host.joinpath("launchers.json").write_text(
        json.dumps([{"id": "demo", "name": "示例应用"}], ensure_ascii=False),
        encoding="utf-8",
    )
    return host, app


def test_load_with_launchers_json_still_merges_metadata(tmp_path):
    host, app = _make_world(tmp_path)

    items = launchers.load(host / "launchers.json", plugin_roots=[str(tmp_path)])

    demo = next(i for i in items if i["id"] == "demo")
    assert demo["cwd"] == str(app)
    assert demo["cmd"].endswith("pythonw.exe")
    assert demo["port"] == 1234


def test_host_config_accepts_both_files(tmp_path):
    host, _ = _make_world(tmp_path)
    assert launchers._host_config(str(host / "config.json"))["plugin_paths"]
    # launchers.json 里没有 look，但它旁边的 config.json 有 —— 不能抛
    assert launchers._host_config(str(host / "launchers.json"))["plugin_paths"]