"""测试要加载的插件入口在哪。

海螺插件 2026-11-08 挪出了仓库，所以不按文件夹名找，而是扫 metadata.json 里的 id。
找的地方：
1. 仓库里内置的 `plugins/conch/main.py`（老位置）
2. 环境变量 `CONCH_PLUGIN_DIR`
3. `~/Desktop/插件`（本机习惯放这儿）
4. `~/.dsh/plugins`、`~/plugins`
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _candidate_dirs() -> list[Path]:
    dirs: list[Path] = []
    env = os.environ.get("CONCH_PLUGIN_DIR")
    if env:
        dirs.append(Path(env))
    # 本机放插件的目录写在这个文件里（它不进仓库：路径带私人信息）
    local = ROOT / "tests" / ".conch_plugin_dir"
    if local.exists():
        try:
            text = local.read_text(encoding="utf-8-sig").strip()
        except Exception:
            text = ""
        if text:
            dirs.append(Path(text))
    home = Path.home()
    dirs += [home / "Desktop" / "插件", home / "plugins", home / ".dsh" / "plugins"]
    return [d for d in dirs if d.is_dir()]


def conch_main() -> Path:
    legacy = ROOT / "plugins" / "conch" / "main.py"
    if legacy.exists():
        return legacy
    for base in _candidate_dirs():
        for meta_path in sorted(base.glob("*/metadata.json")):
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8-sig"))
            except Exception:
                continue
            if str(data.get("id") or "") != "conch":
                continue
            entry = meta_path.parent / "main.py"
            if entry.exists():
                return entry
    raise FileNotFoundError(
        "找不到 id=conch 的插件：设 CONCH_PLUGIN_DIR 指向插件目录，"
        "或把插件放在 ~/Desktop/插件/ 下（需要 metadata.json + main.py）"
    )