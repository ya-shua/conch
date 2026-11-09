"""插件元数据：只有 metadata 没有 main.py 的目录 = 外部应用（kind=app）。

它们由启动器负责起停，加载器只登记、不去 import。
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from core import loader


def _folder(tmp_path: Path, name: str, meta: dict) -> Path:
    d = tmp_path / name
    d.mkdir()
    (d / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return d


def test_no_main_py_is_app(tmp_path):
    d = _folder(tmp_path, "eyes", {"id": "eyes", "name": "留影", "version": "1.8.0"})
    meta = loader.read_meta(d)
    assert meta.kind == "app"
    assert meta.version == "1.8.0"


def test_with_main_py_is_plugin(tmp_path):
    d = _folder(tmp_path, "websearch", {"id": "websearch", "name": "联网"})
    (d / "main.py").write_text("", encoding="utf-8")
    assert loader.read_meta(d).kind == "plugin"


def test_kind_can_be_declared(tmp_path):
    d = _folder(tmp_path, "odd", {"id": "odd", "kind": "app"})
    (d / "main.py").write_text("", encoding="utf-8")
    assert loader.read_meta(d).kind == "app"


def test_scan_lists_app_and_keeps_data_dirs_quiet(tmp_path, caplog):
    """有 metadata 的应用要进列表；纯数据目录不刷 warning。"""
    _folder(tmp_path, "eyes", {"id": "eyes", "name": "留影"})
    (tmp_path / "backups").mkdir()
    (tmp_path / "backups" / "acl.json").write_text("{}", encoding="utf-8")
    # 宿主自己的安装目录躺在插件目录里时（插件都挪到「桌面\插件」之后就是这样），也不能报错。
    # 认宿主的特征是 core/panel/server.py —— 别的插件也有 core/version.py（微光域就是），不能用它当标志。
    host = tmp_path / "conch"
    (host / "core" / "panel").mkdir(parents=True)
    (host / "core" / "panel" / "server.py").write_text("", encoding="utf-8")
    (host / "core" / "version.py").write_text('__version__ = "1.0.2"', encoding="utf-8")
    (host / "main.py").write_text("", encoding="utf-8")
    ld = loader.Loader(tmp_path, make_ctx=lambda meta: object())
    metas = ld.scan()
    assert [m.id for m in metas] == ["eyes"]
    assert ld.list()[0]["kind"] == "app"
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_enable_app_explains_launcher(tmp_path):
    """点「启用」不会被 import，而是直接告诉他去用启动器。"""
    _folder(tmp_path, "eyes", {"id": "eyes", "name": "留影"})
    ld = loader.Loader(tmp_path, make_ctx=lambda meta: object())
    ld.scan()
    with pytest.raises(RuntimeError, match="外部应用"):
        asyncio.run(ld.enable("eyes"))
    assert sys.modules.get("conch_plugin_eyes") is None