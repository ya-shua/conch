"""心情：接 Tide（汐）本体，桥接这边只当适配层。

为什么不再自带引擎
------------------
情绪怎么涨落、什么时候饿、什么时候委屈，这些是 Tide 自己的事。
两处各写一份只会漂移——改了一边另一边不知道。所以这里不留逻辑：

    Tide 在 → 用它；Tide 不在 → 心情不生效（不拦话、不注入），其余照常

两个扩展点：
  speech.prompt   把「当前情绪 / 语气 / 字数上限 / 禁止事项」写进系统提示词
  speech.gate     发出去之前问一句能不能发：勿扰时段、闹脾气时可以拦下
"""

from __future__ import annotations

import importlib.util
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _load_backend():
    spec = importlib.util.spec_from_file_location("qqbridge_tide_backend", _HERE / "tide_backend.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"加载适配层失败：{_HERE / 'tide_backend.py'}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _state_path(ctx) -> Path:
    data_dir = Path(getattr(ctx.cfg, "data_dir", "") or "data")
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "tide_core_state.json"


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
    if not cfg.get("enabled", True):
        ctx.log.info("心情插件关着，跳过")
        return

    path = _state_path(ctx)
    try:
        backend = _load_backend().build(cfg, path)
    except Exception as exc:
        ctx.log.warning("接不上 Tide，心情不生效（说话不受影响）：%s", exc)
        return

    ctx.services["mood"] = {
        "engine": backend,
        "path": path,
        "saved_at": time.time(),
        "count": 0,
    }

    ctx.on("message.observed", lambda p: observe(ctx, p), priority=50)
    ctx.on("speech.prompt", lambda p: prompt_hook(ctx, p), priority=50)
    ctx.on("speech.gate", lambda p: gate_hook(ctx, p), priority=50)

    snap = backend.snapshot("owner")
    ctx.log.info(
        "心情就绪（Tide 本体｜情绪 %s %.2f｜关系 %s｜%s）",
        snap.get("emotion", {}).get("label", "?"),
        snap.get("emotion", {}).get("intensity", 0.0),
        snap.get("bond", {}).get("tier_name", "?"),
        path,
    )


async def teardown(ctx) -> None:
    runtime = ctx.services.pop("mood", None) or {}
    engine = runtime.get("engine")
    if engine is None:
        return
    try:
        save(runtime)
    except Exception as exc:
        ctx.log.warning("保存心情失败：%s", exc)


def save(runtime: dict) -> None:
    runtime["engine"].save_state()
    runtime["saved_at"] = time.time()
    runtime["count"] = 0


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

    bond = snap.get("bond") or {}
    owner = person == "owner"
    allow, reason = True, ""

    if feature == "proactive":
        prox = plan.get("proactive") or {}
        allow = bool(prox.get("should_send"))
        reason = str(prox.get("reason") or "")
    elif snap.get("quiet") and not owner and bool(cfg.get("deny_in_quiet", True)):
        allow, reason = False, "她在睡觉"
    elif bond.get("mood") == "cold" and not owner and bool(cfg.get("block_when_sulky", False)):
        allow, reason = False, "还在闹脾气，不想理人"

    payload["allow"] = allow
    payload["reason"] = reason
    if allow:
        ctx.log.debug(
            "放行 %s 情绪=%s 关系=%s 语气=%s",
            conv_id,
            (snap.get("emotion") or {}).get("label"),
            bond.get("tier_name"),
            plan.get("tone"),
        )
        if feature == "proactive":
            engine.mark_proactive(person)
        else:
            engine.mark_replied(person)
    else:
        ctx.log.info("拦下一条（%s / %s）：%s", conv_id, bond.get("mood"), reason or "没给理由")