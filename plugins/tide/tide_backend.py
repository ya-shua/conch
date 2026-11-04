"""把 Tide（汐）本体接成心情后端。

为什么要有这一层
----------------
海螺自己要能独立跑（仓库里带着一份 engine.py），但本机已经有 Tide 本体
（情绪/需求/生物钟/关系都在里面，18 个模块），没必要维护两套一样的逻辑。
所以这里只做「翻译」：把 TideCore 的接口对齐成 engine.MoodEngine 的形状，
main.py 一行都不用分叉。

配置：
    "engine": "tide"        # builtin（默认，用自带那份）| tide
    "tide_home": "…\\tide"   # Tide 项目根
"""

from __future__ import annotations

import sys
from pathlib import Path


class TideBackend:
    """接口对齐 engine.MoodEngine，供 main.py 无差别调用。"""

    name = "tide"

    def __init__(self, core, state_path: Path):
        self.core = core
        self.state_path = state_path

    # ── 与 MoodEngine 同名的方法 ─────────────────────────────

    def observe(self, text: str, person: str, speaker: str = "other") -> None:
        # Tide 的参数顺序是 (text, speaker, person)，别照抄
        self.core.observe(text, speaker=speaker, person=person)

    def plan(self, person: str) -> dict:
        self.core.tick()
        return self.core.plan_reply(person) or {}

    def snapshot(self, person: str) -> dict:
        return self.core.snapshot(person) or {}

    def mark_proactive(self, person: str) -> None:
        try:
            self.core.send_proactive()
        except Exception:
            pass

    def mark_replied(self, person: str) -> None:
        # Tide 在 observe 里自己维护话题，这里不用动
        return None

    # ── 存盘：交给 Tide 自己那份状态文件 ─────────────────────

    def save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.core.save()


def build(cfg: dict, state_path: Path):
    """按配置造一个 Tide 后端；找不到 Tide 就返回 None，让上层回退。"""
    home = str(cfg.get("tide_home") or "").strip()
    if not home:
        raise RuntimeError("没配 tide_home（Tide 项目根目录）")
    root = Path(home)
    if not (root / "tide" / "core.py").exists():
        raise RuntimeError(f"{root} 下没有 tide/core.py")

    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from tide.core import TideCore  # noqa: PLC0415

    config_dir = root / "config"
    core = TideCore.from_config(config_dir, state_path=state_path)
    if state_path.exists():
        core.load()
    return TideBackend(core, state_path)