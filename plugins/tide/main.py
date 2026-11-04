"""心情插件：她自己的情绪、需求、关系。

不依赖任何外部程序，状态存在本宿主的 data 目录里。

两个扩展点：
  speech.prompt   把「当前情绪 / 语气 / 字数上限 / 禁止事项」写进系统提示词
  speech.gate     发出去之前问一句能不能发：勿扰时段、闹脾气时可以拦下

想让她主动冒泡的功能，问 plan()["proactive"]["should_send"] 就行。
"""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _load_engine():
    """插件是单文件加载的，没有包，所以引擎按路径单独加载一次。"""
    spec = importlib.util.spec_from_file_location("qqbridge_tide_engine", _HERE / "engine.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"加载引擎失败：{_HERE / 'engine.py'}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ENGINE = _load_engine()
MoodEngine = _ENGINE.MoodEngine

def _state_path(ctx) -> Path:
    data_dir = Path(getattr(ctx.cfg, "data_dir", "") or "data")
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "tide_state.json"


def person_for(ctx, conv_id: str, sender_id: str = "") -> str:
    """主人私聊算「主人」本人；其余会话各自独立，每个群有自己的关系。"""
    owner = str(ctx.cfg.owner_qq or "")
    if conv_id.startswith("private:") and owner:
        sid = sender_id or conv_id.split(":", 1)[1]
        if str(sid) == owner:
            return "owner"
    return conv_id or "group"


async def setup(ctx) -> None:
    cfg = dict(ctx.config or {})
    engine = MoodEngine(cfg)
    path = _state_path(ctx)
    loaded = False
    if path.exists():
        try:
            engine.load_state(json.loads(path.read_text(encoding="utf-8")))
            loaded = True
        except Exception as exc:
            ctx.log.warning("心情状态读不了，先当新的开始：%s", exc)

    runtime = {"engine": engine, "path": path, "saved_at": time.time(), "count": 0, "dirty": False}
    ctx.services["mood"] = runtime

    ctx.on("message.observed", lambda p: observe(ctx, p), priority=50)
    ctx.on("speech.prompt", lambda p: prompt_hook(ctx, p), priority=50)
    ctx.on("speech.gate", lambda p: gate_hook(ctx, p), priority=50)

    snap = engine.snapshot("owner")
    ctx.log.info(
        "心情就绪（%s情绪 %s %.2f｜关系 %s｜%s）",
        "已恢复，" if loaded else "全新，",
        snap["emotion"]["label"],
        snap["emotion"]["intensity"],
        snap["bond"]["tier_name"],
        str(path),
    )


async def teardown(ctx) -> None:
    runtime = ctx.services.pop("mood", None) or {}
    engine = runtime.get("engine")
    path = runtime.get("path")
    if engine is None or path is None:
        return
    try:
        save(runtime)
    except Exception as exc:
        ctx.log.warning("保存心情失败：%s", exc)


def save(runtime: dict) -> None:
    engine = runtime["engine"]
    path: Path = runtime["path"]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(engine.to_state(), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    runtime["saved_at"] = time.time()
    runtime["count"] = 0
    runtime["dirty"] = False


def _maybe_save(ctx, runtime: dict) -> None:
    every = max(1, int((ctx.config or {}).get("save_every") or 20))
    if runtime["count"] >= every:
        save(runtime)


def observe(ctx, payload: dict) -> None:
    """每来一条消息都让她经历一次，情绪会跟着涨落。"""
    text = str(payload.get("text") or "").strip()
    if not text:
        return
    runtime = ctx.services.get("mood")
    if not runtime:
        return
    person = person_for(ctx, str(payload.get("conv_id") or ""), str(payload.get("sender_id") or ""))
    speaker = str(payload.get("speaker") or "other")
    try:
        runtime["engine"].observe(text, person, speaker=speaker)
    except Exception as exc:
        ctx.log.warning("observe 失败：%s", exc)
        return
    runtime["count"] += 1
    _maybe_save(ctx, runtime)


async def prompt_hook(ctx, payload: dict) -> None:
    """把心情写进提示词，她今天什么状态，回复里就带出来。"""
    runtime = ctx.services.get("mood")
    if not runtime:
        return
    person = person_for(ctx, str(payload.get("conv_id") or ""), str(payload.get("sender_id") or ""))
    try:
        plan = runtime["engine"].plan(person)
    except Exception as exc:
        ctx.log.warning("plan 失败：%s", exc)
        return
    payload["extra"] = str(payload.get("extra") or "") + "\n\n" + str(plan.get("prompt") or "")
    payload["plan"] = plan


async def gate_hook(ctx, payload: dict) -> None:
    """放行判定。默认放行，只有明确的理由才拦。"""
    runtime = ctx.services.get("mood")
    if not runtime:
        return
    engine = runtime["engine"]
    cfg = ctx.config or {}
    conv_id = str(payload.get("conv_id") or "")
    sender_id = str(payload.get("sender_id") or "")
    feature = str(payload.get("feature") or "chat")
    person = person_for(ctx, conv_id, sender_id)

    try:
        snap = engine.snapshot(person)
        plan = engine.plan(person)
    except Exception as exc:
        ctx.log.warning("心情读取失败，先放行：%s", exc)
        return

    bond = snap["bond"]
    owner = person == "owner"
    allow, reason = True, ""

    if feature == "proactive":
        prox = plan.get("proactive") or {}
        allow = bool(prox.get("should_send"))
        reason = str(prox.get("reason") or "")
    elif snap["quiet"] and not owner and bool(cfg.get("deny_in_quiet", True)):
        allow, reason = False, "她在睡觉"
    elif bond.get("mood") == "cold" and not owner and bool(cfg.get("block_when_sulky", False)):
        allow, reason = False, "还在闹脾气，不想理人"

    payload["allow"] = allow
    payload["reason"] = reason
    if allow:
        ctx.log.debug(
            "放行 %s 情绪=%s 关系=%s 语气=%s",
            conv_id, snap["emotion"]["label"], bond.get("tier_name"), plan.get("tone"),
        )
        if feature == "proactive":
            engine.mark_proactive(person)
        else:
            engine.mark_replied(person)
    else:
        ctx.log.info("拦下一条（%s / %s）：%s", conv_id, bond.get("mood"), reason or "没给理由")