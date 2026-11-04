"""留影接进海螺：消息里带图时，先看一眼，再把看到的东西交回给提示词。

为什么用子进程而不是 HTTP
------------------------
留影自己有面板（:5188），但那条链路是「监听消息流」的，不是「给张图、要一句话」。
而且面板挂了不该让她连话都不会说 —— 子进程失败就当没看见图。

看图这件事本身仍由留影的代码完成（ring-vision.mjs / describe.mjs），
这里只借它的取图和描述，别把它的提示词抄一份过来。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

_HERE = Path(__file__).parent
_SCRIPT = _HERE.parent.parent / "bridge" / "eye.mjs"


def _cfg(ctx) -> dict:
    return dict(ctx.config or {})


async def setup(ctx) -> None:
    cfg = _cfg(ctx)
    if not cfg.get("enabled", True):
        return
    ctx.on("speech.prompt", lambda payload: _on_prompt(ctx, payload), priority=20)
    ctx.log.info("留影就绪（视觉模型 %s）", cfg.get("model") or "跟主模型同一份凭据")


async def teardown(_ctx) -> None:
    return


async def _on_prompt(ctx, payload: dict) -> None:
    images = payload.get("images") or []
    if not images:
        return
    limit = int(_cfg(ctx).get("max_images") or 2)
    seen: list[str] = []
    for url in images[:limit]:
        text = await look(ctx, url)
        if text:
            seen.append(text)
    if seen:
        payload["extra"] = str(payload.get("extra") or "") + "\n\n" + "\n".join(seen)


async def look(ctx, url: str) -> str:
    """看一张图，返回一句能塞进提示词的话；任何失败都返回空字符串。"""
    cfg = _cfg(ctx)
    node = _resolve_node(cfg)
    script = _resolve_script(cfg)
    if not node or not script:
        return ""

    env = dict(os.environ)
    base = str(cfg.get("base_url") or getattr(ctx.cfg.model, "base_url", "") or "")
    model = str(cfg.get("model") or "")
    key = str(cfg.get("api_key") or getattr(ctx.cfg.model, "api_key", "") or "")
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
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, OSError) as exc:
        ctx.log.warning("看图失败（%s）：%s", url[:60], exc)
        return ""

    lines = out.decode("utf-8", "replace").strip().splitlines()
    if not lines:
        return ""
    try:
        data = json.loads(lines[-1])
    except json.JSONDecodeError:
        return ""
    if not data.get("ok"):
        ctx.log.warning("看图没看成：%s", str(data.get("error"))[:120])
        return ""

    desc = str(data.get("desc") or "").strip()
    points = [str(p).strip() for p in (data.get("points") or []) if str(p).strip()]
    if not desc:
        return ""
    line = f"（有人发了张图，你看到的是：{desc}"
    if points:
        line += "；要点：" + "、".join(points[:4])
    return line + "）"


def _resolve_node(cfg: dict) -> str:
    """优先用插件配置里的 node，其次 PATH —— 不在代码里写死谁家的路径。"""
    node = str(cfg.get("node") or "").strip()
    if node and Path(node).exists():
        return node
    return shutil.which("node") or shutil.which("node.exe") or ""


def _resolve_script(cfg: dict) -> str:
    raw = str(cfg.get("script") or "").strip()
    path = Path(raw) if raw else _SCRIPT
    return str(path) if path.exists() else ""