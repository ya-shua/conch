"""QQ 接入插件。

一条消息的完整路径：
  收到 → 白名单 → 合流窗口 → 拼人设 → 调模型 → 发回 → 记账

两个扩展点（给别的插件插话用）：
  speech.prompt  调模型之前，别的插件可以往系统提示词里加东西（心情、语气、长度上限）
  speech.gate    发出去之前，别的插件可以否决（心情不对就不许说）

遥控：
  主人私聊里说"去某个群说一句 X"，她通过 <SEND 群名>内容</SEND> 真的发到群里，
  不需要那个群有人在说话。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

from qqbridge.core.config import norm_id
from qqbridge.core.message import Message, Segment
from qqbridge.core.pipeline import Merger
from qqbridge.core.policy import allowed

DEFAULT_SYSTEM = (
    "你是一个在 QQ 上聊天的人，不是客服。"
    "回复要短，像真人打字，不要用列表和标题。"
    "不知道就说不知道，不要编。"
)


def system_prompt(ctx) -> str:
    """人设卡：config 里 persona 指定名字，没写就用 default。"""
    base = Path(ctx.cfg.roles_dir)
    name = str((ctx.cfg.raw or {}).get("persona") or "default").strip() or "default"
    for candidate in (base / f"{name}.md", base / "default.md"):
        try:
            if candidate.exists():
                text = candidate.read_text(encoding="utf-8").strip()
                if text:
                    return text
        except OSError as exc:
            ctx.log.warning("读人设失败：%s", exc)
    return DEFAULT_SYSTEM


_CQ_RE = re.compile(r"\[CQ:([a-zA-Z_]+)((?:,[^\]]*)?)\]")


def to_segments(text: str) -> list[Segment]:
    """把模型吐出来的 CQ 码还原成真正的表情段，其余 CQ 码直接丢掉。"""
    out: list[Segment] = []
    pos = 0
    for match in _CQ_RE.finditer(text):
        head = text[pos : match.start()]
        if head:
            out.append(Segment("text", {"text": head}))
        kind = match.group(1).lower()
        params = {}
        for item in (match.group(2) or "").split(","):
            if "=" in item:
                k, v = item.split("=", 1)
                params[k.strip()] = v.strip()
        if kind == "face" and params.get("id"):
            out.append(Segment("face", {"id": params["id"]}))
        elif kind == "text" and params.get("text"):
            out.append(Segment("text", {"text": params["text"]}))
        pos = match.end()
    tail = text[pos:]
    if tail:
        out.append(Segment("text", {"text": tail}))

    cleaned = [seg for seg in out if seg.type != "text" or seg.data.get("text")]
    return cleaned or [Segment("text", {"text": text})]


def speaker_label(msg: Message, owner: str) -> str:
    """群里是爸在说话就打标记 —— 人设卡第零条靠这个认人，别让人冒充。"""
    if msg.kind == "group" and owner and norm_id(msg.sender_id) == owner:
        return f"【管理员】{msg.sender_name}"
    return msg.sender_name


def render_batch(batch: list[Message], owner: str = "") -> str:
    """把合流窗口里的几条消息拼成一段上下文。"""
    if len(batch) == 1:
        msg = batch[0]
        if msg.kind == "group":
            return f"[{speaker_label(msg, owner)}] {msg.text}"
        return record_owner_prefix(msg, owner) + msg.text
    return "\n".join(f"[{speaker_label(m, owner)}] {m.text}" for m in batch)


def record_owner_prefix(msg: Message, owner: str) -> str:
    """私聊里给主人的话也标一下，人设卡两种场景都认得出。"""
    if owner and norm_id(msg.sender_id) == owner:
        return "【管理员】"
    return ""


async def setup(ctx) -> None:
    merger = Merger(ctx.cfg.merge_window_ms, lambda batch: handle_batch(ctx, batch))
    ctx.services["merger"] = merger
    ctx.on("message.received", lambda msg: on_message(ctx, merger, msg), priority=10)
    await _setup_tools(ctx)
    asyncio.create_task(_pull_groups_when_ready(ctx))
    ctx.log.info("海螺就绪（合流窗口 %sms）", ctx.cfg.merge_window_ms)


async def _pull_groups_when_ready(ctx, tries: int = 60) -> None:
    """协议端连上之后再拉群列表 —— 面板靠它显示"她能看到的群"。"""
    for _ in range(tries):
        client = ctx.services.get("onebot")
        if client is not None and getattr(client, "connected", False):
            await ensure_groups(ctx)
            return
        await asyncio.sleep(5)


async def _setup_tools(ctx) -> None:
    """把桌面上的 MCP 插件接成她能调用的工具。没配就什么都不做。"""
    spec = (ctx.cfg.raw or {}).get("tools") or {}
    servers = spec.get("servers") or []
    if not spec.get("enabled") or not servers:
        return
    try:
        from qqbridge.core.mcp import McpPool

        pool = McpPool(servers)
        await pool.start()
        ctx.services["mcppool"] = pool
        total = sum(len(srv._tools) for srv in pool.servers.values())
        ctx.log.info("工具接好了：%s 个（%s）", total, "、".join(pool.servers))
    except Exception as exc:
        ctx.log.warning("工具接入失败：%s", exc)


async def teardown(ctx) -> None:
    pool = ctx.services.pop("mcppool", None)
    if pool is not None:
        try:
            await pool.close()
        except Exception:
            pass
    merger = ctx.services.pop("merger", None)
    if merger is not None:
        await merger.close()
    reset_state()


_STATE: dict[str, dict] = {}


def _st(conv: str) -> dict:
    return _STATE.setdefault(
        conv, {"last_reply": 0.0, "hour": "", "hour_count": 0, "consecutive": 0}
    )


def should_reply(ctx, msg: Message) -> bool:
    st = ctx.cfg.strategy or {}
    if st.get("participate") == "listen_only":
        return False
    trigger = st.get("trigger", "at_only")
    if msg.kind == "private":
        return True
    if trigger == "any":
        return True
    if trigger == "at_or_name":
        name = str((ctx.cfg.raw or {}).get("bot_name") or "").strip()
        return msg.at_me or bool(name and name in msg.text)
    return msg.at_me


def rate_limited(ctx, conv: str) -> bool:
    st = ctx.cfg.strategy or {}
    now = time.time()
    state = _st(conv)

    cooldown = int(st.get("cooldown_sec") or 0)
    if cooldown and now - state["last_reply"] < cooldown:
        return True

    hour = time.strftime("%Y%m%d%H", time.localtime(now))
    if state["hour"] != hour:
        state["hour"], state["hour_count"] = hour, 0
    if state["hour_count"] >= int(st.get("max_per_hour") or 30):
        return True
    return False


def mark_replied(conv: str) -> None:
    state = _st(conv)
    state["last_reply"] = time.time()
    state["hour_count"] += 1
    state["consecutive"] += 1


def reset_state() -> None:
    _STATE.clear()


def lexicon_block(ctx) -> str:
    """把词库里的黑话注进提示词，让她听得懂群里的梗。"""
    try:
        items = ctx.store.lexicon_list()
    except Exception:
        return ""
    slang = [i for i in items if i.get("kind") == "slang"][:40]
    if not slang:
        return ""
    lines = []
    for item in slang:
        note = (item.get("note") or "").strip()
        lines.append(f"- {item['term']}：{note}" if note else f"- {item['term']}")
    return "\n\n群里常说的词（按这个理解，别主动解释它们）：\n" + "\n".join(lines)


async def talk(ctx, messages: list[dict], scope: str) -> tuple[str, dict[str, int]]:
    """有工具就走工具循环，没有就一句话返回。"""
    backend = ctx.services["backend"]
    pool = ctx.services.get("mcppool")
    tools = pool.tools_for(scope) if pool is not None else []
    if not tools:
        return await backend.chat(messages)

    rounds = int(((ctx.cfg.raw or {}).get("tools") or {}).get("max_rounds") or 4)
    total = {"prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0}
    msgs = list(messages)

    for _ in range(max(1, rounds)):
        message, usage = await backend.chat_raw(msgs, tools=tools)
        for k in total:
            total[k] += int(usage.get(k) or 0)

        calls = message.get("tool_calls") or []
        if not calls:
            return str(message.get("content") or "").strip(), total

        msgs.append(
            {
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": calls,
            }
        )
        for call in calls:
            fn = call.get("function") or {}
            name = str(fn.get("name") or "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            ctx.log.info("调工具 %s(%s)", name, str(args)[:120])
            result = await pool.call(name, args if isinstance(args, dict) else {})
            msgs.append(
                {"role": "tool", "tool_call_id": str(call.get("id") or ""), "content": result}
            )

    # 轮数用完，逼她说句人话（不带工具）
    message, usage = await backend.chat_raw(msgs)
    for k in total:
        total[k] += int(usage.get(k) or 0)
    return str(message.get("content") or "").strip(), total


def needs_approval(ctx, text: str) -> bool:
    """命中了「关键词」类词条，又开了审批开关，就先挂起等主人点头。"""
    strategy = ctx.cfg.strategy or {}
    if not strategy.get("confirm_uncertain"):
        return False
    try:
        keywords = [i["term"] for i in ctx.store.lexicon_list() if i.get("kind") == "keyword"]
    except Exception:
        return False
    return any(k and k in text for k in keywords)


# ── 给别的插件留的扩展点 ──────────────────────────────────────


def image_urls(batch) -> list[str]:
    """把这一批消息里的图链挑出来，交给「眼睛」类插件去看。"""
    urls: list[str] = []
    for msg in batch:
        for seg in getattr(msg, "segments", []) or []:
            if getattr(seg, "type", "") != "image":
                continue
            data = getattr(seg, "data", None) or {}
            url = str(data.get("url") or data.get("file") or "")
            if url.startswith("http") and url not in urls:
                urls.append(url)
    return urls[:4]


async def ask_prompt_extra(
    ctx,
    conv_id: str,
    text: str,
    feature: str = "chat",
    sender_id: str = "",
    images: list[str] | None = None,
) -> str:
    """让别的插件往系统提示词里加东西（心情、语气、长度上限……）。"""
    payload = {
        "conv_id": conv_id,
        "sender_id": sender_id,
        "text": text,
        "feature": feature,
        "images": list(images or []),
        "extra": "",
    }
    try:
        await ctx.bus.emit("speech.prompt", payload)
    except Exception as exc:
        ctx.log.warning("speech.prompt 出错：%s", exc)
    return str(payload.get("extra") or "")


async def ask_gate(
    ctx, conv_id: str, text: str, feature: str = "chat", sender_id: str = ""
) -> tuple[bool, str]:
    """发出去之前问一句：这句能不能发。没插件管就默认放行。"""
    payload = {
        "conv_id": conv_id,
        "sender_id": sender_id,
        "text": text,
        "feature": feature,
        "allow": True,
        "reason": "",
    }
    try:
        await ctx.bus.emit("speech.gate", payload)
    except Exception as exc:
        ctx.log.warning("speech.gate 出错：%s", exc)
    return bool(payload.get("allow", True)), str(payload.get("reason") or "")


# ── 遥控：主人私聊指挥她往群里发话 ─────────────────────────────
# 不需要群里有人说话。私聊是遥控器，群是喇叭。
# 只认 owner，白名单外的群一律拒发。

REMOTE_HELP = """

你还能替主人往群里发消息。
主人私聊你、让你去某个群说点什么时（哪怕那个群一个人都没说话），用这个格式：

<SEND 群号或群名>你要发的话</SEND>

群名写主人说的那个名字就行，我会自己找对应的群号。
如果主人只说了"去群里说一句"，没指哪个群，就问他去哪个群，不要猜。
发完之后用一句话告诉主人结果，不要把内容再重复一遍。
不要在没有被要求的时候主动往群里发东西。"""

SEND_RE = re.compile(r"<SEND\s+([^<>\n]{1,40})>(.*?)</SEND>", re.S)
_GROUPS: dict[str, str] = {}  # 群号 -> 群名


def is_owner_private(msg: Message, cfg) -> bool:
    return (
        msg.kind == "private"
        and bool(cfg.owner_qq)
        and norm_id(msg.sender_id) == cfg.owner_qq
    )


async def ensure_groups(ctx) -> None:
    """拉一次群列表，用来把群名对应到群号。"""
    if _GROUPS:
        return
    client = ctx.services.get("onebot")
    if client is None:
        return
    try:
        data = await client.call("get_group_list")
    except Exception as exc:
        ctx.log.warning("拉群列表失败：%s", exc)
        return
    for item in data or []:
        gid = norm_id(item.get("group_id"))
        name = str(item.get("group_name") or "").strip()
        if gid:
            _GROUPS[gid] = name
    try:
        ctx.store.kv_put(ctx.id, "groups", dict(_GROUPS))
    except Exception:
        pass
    ctx.log.info("拉到 %s 个群", len(_GROUPS))


async def resolve_group(ctx, target: str) -> str | None:
    """群号直接认；群名做包含匹配。"""
    target = (target or "").strip().strip("「」\"'")
    if target.isdigit():
        return target
    await ensure_groups(ctx)
    if not target:
        return None
    for gid, name in _GROUPS.items():
        if name and name == target:
            return gid
    for gid, name in _GROUPS.items():
        if name and (target in name or name in target):
            return gid
    return None


def group_name(gid: str) -> str:
    return _GROUPS.get(gid) or gid


async def send_to_group(ctx, target: str, text: str) -> tuple[bool, str]:
    gid = await resolve_group(ctx, target)
    if not gid:
        names = "、".join(f"{n}（{g}）" for g, n in list(_GROUPS.items())[:8]) or "还没拉到群列表"
        return False, f"找不到群「{target}」。我知道的群：{names}"
    conv = f"group:{gid}"
    if not allowed(ctx.cfg, conv, ""):
        return False, f"群「{group_name(gid)}」不在白名单里，没发。"

    ok, why = await ask_gate(ctx, conv, text, feature="remote")
    if not ok:
        return False, f"「{group_name(gid)}」没发出去：{why or '被心情拦下了'}"

    try:
        await ctx.send(conv, [Segment("text", {"text": text})])
    except Exception as exc:
        ctx.log.error("遥控发送失败 %s：%s", conv, exc)
        return False, f"发到「{group_name(gid)}」失败：{exc}"
    try:
        ctx.store.message_add(conv, "out", text, plugin_id=ctx.id)
    except Exception:
        pass
    ctx.log.info("遥控发群 %s：%s", conv, text[:40])
    return True, f"已发到「{group_name(gid)}」"


async def run_remote(ctx, reply: str) -> str:
    """把回复里的 <SEND> 执行掉，剩下的话才是私聊要说的话。"""
    if "<SEND" not in reply:
        return reply
    notes: list[str] = []
    for match in SEND_RE.finditer(reply):
        target = match.group(1).strip()
        text = match.group(2).strip()
        if not text:
            continue
        try:
            ok, detail = await send_to_group(ctx, target, text)
        except Exception as exc:
            ok, detail = False, f"发送出错：{exc}"
        notes.append(("✓ " if ok else "✗ ") + detail)
    rest = SEND_RE.sub("", reply).strip()
    if notes:
        tail = "\n".join(notes)
        return (rest + "\n\n" + tail).strip() if rest else tail
    return rest or reply


# ── 主流程 ────────────────────────────────────────────────────


async def on_message(ctx, merger: Merger, msg: Message) -> None:
    if not allowed(ctx.cfg, msg.conv_id, msg.sender_id):
        ctx.log.debug("不在白名单，忽略：%s / %s", msg.conv_id, msg.sender_id)
        return None
    if not msg.text:
        return None  # M1 只处理文本

    try:
        ctx.store.message_add(
            msg.conv_id,
            "in",
            msg.text,
            sender_id=msg.sender_id,
            sender_name=msg.sender_name,
            message_id=str(msg.message_id),
            plugin_id=ctx.id,
        )
    except Exception as exc:
        ctx.log.warning("写聊天记录失败：%s", exc)

    # 让别的插件观察这条消息（心情、记忆之类）
    try:
        await ctx.bus.emit(
            "message.observed",
            {
                "conv_id": msg.conv_id,
                "kind": msg.kind,
                "sender_id": msg.sender_id,
                "sender_name": msg.sender_name,
                "text": msg.text,
            },
        )
    except Exception as exc:
        ctx.log.debug("message.observed 出错：%s", exc)

    if not should_reply(ctx, msg):
        return None
    if rate_limited(ctx, msg.conv_id):
        ctx.log.info("频率闸拦下一条：%s", msg.conv_id)
        return None
    merger.feed(msg)
    return None


async def handle_batch(ctx, batch: list[Message]) -> None:
    conv_id = batch[0].conv_id
    turn_id = batch[-1].message_id
    model = ctx.cfg.model.model
    user_text = render_batch(batch, str(ctx.cfg.owner_qq or ""))
    remote = is_owner_private(batch[0], ctx.cfg)

    system = system_prompt(ctx) + lexicon_block(ctx)
    if remote:
        system += REMOTE_HELP
    system += await ask_prompt_extra(
        ctx,
        conv_id,
        user_text,
        feature="chat",
        sender_id=batch[0].sender_id,
        images=image_urls(batch),
    )

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_text},
    ]

    try:
        reply, usage = await talk(ctx, messages, "owner" if remote else "all")
    except Exception as exc:
        ctx.log.error("模型调用失败 conv=%s：%s", conv_id, exc)
        return

    if not reply:
        ctx.log.info("模型返回空，不发送 conv=%s", conv_id)
        return

    if remote:
        reply = await run_remote(ctx, reply)
        if not reply:
            ctx.log.info("遥控指令已执行，私聊没有别的话：%s", conv_id)
            return

    if not remote and needs_approval(ctx, user_text):
        ctx.store.approval_add(
            conv_id,
            user_text[:200],
            {"reply": reply},
            turn_id=str(turn_id),
        )
        ctx.log.info("命中关键词，挂起等审批：%s", conv_id)
        return

    ok, why = await ask_gate(ctx, conv_id, reply, feature="chat", sender_id=batch[0].sender_id)
    if not ok:
        ctx.log.info("没发出去（%s）：%s", why or "被拦", reply[:40])
        return

    try:
        await ctx.send(conv_id, to_segments(reply))
    except Exception as exc:
        ctx.log.error("发送失败 conv=%s：%s", conv_id, exc)
        return
    try:
        ctx.store.message_add(conv_id, "out", reply, plugin_id=ctx.id)
    except Exception as exc:
        ctx.log.warning("写回复记录失败：%s", exc)
    mark_replied(conv_id)

    record = ctx.ledger.record(
        feature="chat",
        conv_id=conv_id,
        turn_id=turn_id,
        model=model,
        usage=usage,
    )
    ctx.log.info(
        "已回复 %s（%s tok，¥%.4f%s）",
        conv_id,
        record["prompt_tokens"] + record["completion_tokens"],
        record["cost_yuan"],
        " 估价" if record["estimated"] else "",
    )