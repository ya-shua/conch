"""看一眼：消息里带图时，借「留影」的看图能力，把看到的东西塞进提示词。

这不是插件 —— 留影是外面那套程序，海螺只是调用方。
`bridge/eye.mjs` 负责跟留影说话（取图 + 描述），这里负责在合适的时机调它。

为什么用子进程而不是 HTTP
------------------------
留影自己有面板（:5188），但那条链路是「监听消息流」的，不是「给张图、要一句话」。
而且面板挂了不该让她连话都不会说 —— 子进程失败就当没看见图。

看图这件事本身仍由留影的代码完成（ring-vision.mjs / describe.mjs），
这里只借它的取图和描述，别把它的提示词抄一份过来。

配置在 `config.json` 的 `look` 块（一般不用动；模型凭据不给就用主模型那份）。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any

from . import log
from .bus import Bus
from .config import Config

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "max_images": 2,
    "timeout_s": 45,
    "node": "",
    "script": "",
    "eye_root": "",
    "base_url": "",
    "model": "",
    "api_key": "",
}


class Look:
    def __init__(self, cfg: Config, bus: Bus | None = None) -> None:
        self.cfg = cfg
        self.bus = bus
        self.log = log.get("look")

    # ---------- 设置 ----------

    @property
    def raw(self) -> dict[str, Any]:
        got = (self.cfg.raw or {}).get("look")
        if not isinstance(got, dict):
            got = (self.cfg.raw or {}).get("eyes")  # 老写法（插件时代）也认
        return got if isinstance(got, dict) else {}

    def settings(self) -> dict[str, Any]:
        out = dict(DEFAULTS)
        for key, val in self.raw.items():
            if key in DEFAULTS:
                out[key] = val
        return out

    def apply(self, values: dict[str, Any]) -> dict[str, Any]:
        block = self.raw.copy()
        for key in DEFAULTS:
            if key in values:
                block[key] = values[key]
        self.cfg.raw["look"] = block
        return self.settings()

    # ---------- 路径 ----------

    def _node(self, cfg: dict) -> str:
        node = str(cfg.get("node") or "").strip()
        if node and Path(node).exists():
            return node
        return shutil.which("node") or shutil.which("node.exe") or ""

    def _script(self, cfg: dict) -> str:
        raw = str(cfg.get("script") or "").strip()
        path = Path(raw) if raw else Path(__file__).resolve().parents[1] / "bridge" / "eye.mjs"
        return str(path) if path.exists() else ""

    # ---------- 看一张图 ----------

    async def look(self, url: str) -> str:
        """看一张图，返回一句能塞进提示词的话；任何失败都返回空字符串。"""
        cfg = self.settings()
        node = self._node(cfg)
        script = self._script(cfg)
        if not node or not script:
            return ""

        # 凭据只认 look 块里写死的：没写就完全交给留影自己的配置。
        # （早先这里会回落到主模型的 key，结果拿 DeepSeek 的 key 去请求留影配的智谱地址，
        #   回来一串 403「Access to model denied」，看着像留影坏了。）
        env = dict(os.environ)
        base = str(cfg.get("base_url") or "")
        model = str(cfg.get("model") or "")
        key = str(cfg.get("api_key") or "")
        if base:
            env["EYE_BASE_URL"] = base
        if model:
            env["EYE_MODEL"] = model
        if key:
            env["EYE_API_KEY"] = key
        if cfg.get("eye_root"):
            env["EYE_ROOT"] = str(cfg["eye_root"])
        env["PYTHONIOENCODING"] = "utf-8"

        timeout = float(cfg.get("timeout_s") or 45)
        try:
            proc = await asyncio.create_subprocess_exec(
                node,
                str(script),
                url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
                creationflags=0x08000000,  # CREATE_NO_WINDOW：别弹控制台
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except (asyncio.TimeoutError, OSError) as exc:
            self.log.warning("看图失败（%s）：%s", url[:60], exc)
            return ""

        lines = out.decode("utf-8", "replace").strip().splitlines()
        if not lines:
            return ""
        try:
            data = json.loads(lines[-1])
        except json.JSONDecodeError:
            return ""
        if not data.get("ok"):
            self.log.warning("看图没看成：%s", str(data.get("error"))[:120])
            return ""

        desc = str(data.get("desc") or "").strip()
        points = [str(p).strip() for p in (data.get("points") or []) if str(p).strip()]
        if not desc:
            return ""
        line = f"（有人发了张图，你看到的是：{desc}"
        if points:
            line += "；要点：" + "、".join(points[:4])
        return line + "）"

    # ---------- 总线 ----------

    async def on_prompt(self, payload: dict) -> None:
        cfg = self.settings()
        if not cfg.get("enabled", True):
            return
        images = payload.get("images") or []
        if not images:
            return
        limit = int(cfg.get("max_images") or 2)
        seen: list[str] = []
        for url in images[:limit]:
            text = await self.look(url)
            if text:
                seen.append(text)
        if seen:
            payload["extra"] = str(payload.get("extra") or "") + "\n\n" + "\n".join(seen)

    def attach(self, bus: Bus) -> None:
        self.bus = bus
        bus.on("speech.prompt", self.on_prompt, priority=20, owner="look")
        cfg = self.settings()
        where = "有图就看" if cfg.get("enabled", True) else "关着，不看图"
        self.log.info("留影接好了（%s；%s）", where, ("模型 " + str(cfg["model"])) if cfg.get("model") else "用留影面板里配的视觉模型")