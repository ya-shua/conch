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
import random
import shutil
import re
import time
import urllib.request
from pathlib import Path

import aiohttp
from dataclasses import replace
from core.config import DEFAULT_CLOSING_WORDS, norm_id
from core.message import Message, Segment, make_text
from core.pipeline import Merger
from core.policy import allowed

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
                    return f"{text}\n\n{VOICE_RULE}"
        except OSError as exc:
            ctx.log.warning("读人设失败：%s", exc)
    return f"{DEFAULT_SYSTEM}\n\n{VOICE_RULE}"


_CQ_RE = re.compile(r"\[CQ:([a-zA-Z_]+)((?:,[^\]]*)?)\]")

# 模型想发表情时写的一行： 【表情包：猫猫-歪头】 / [表情：x]
_STICKER_LINE = re.compile(r"^[【\[]\s*表\s*情(?:包)?\s*[:：]\s*(.+?)\s*[】\]]$")
_STICKER_ANY = re.compile(r"[【\[]\s*表\s*情(?:包)?\s*[:：][^】\]]*[】\]]")

# 模型发语音时写的一行：<audio src="<语音文件路径>\\x.wav"></audio>
# voice_tts 只返回文件路径，模型不会自己拼 record 段，所以这里把标签翻成真语音。
_AUDIO_TAG = re.compile(
    r"<audio\b[^>]*?\bsrc\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s>\"']+))[^>]*>(?:\s*</audio>)?",
    re.I,
)
# 兜底：模型直接甩一行本地音频路径（没写标签）也认
_AUDIO_PATH_LINE = re.compile(
    r"^[A-Za-z]:[\\/][^\n]+\.(?:wav|mp3|m4a|flac|amr|silk|ogg)$", re.I
)

# 每个会话最近说过的话：只给模型看，不落库（上限 20 条 / 1800 字）
_HISTORY: dict[str, list[dict]] = {}
_HISTORY_CHARS = 1800
_HISTORY_KEEP = 20
# 重启后从库里捡回历史：超过这个时间的旧聊天不捡（隔夜的话捡回来反而串味儿）
_HISTORY_SEED_HOURS = 24
# 「刚才聊了什么」往回看多久：隔了一整夜（超过这么久）就当话题翻篇了，
# 别把昨天的对话当刚说完的接着接。
_HISTORY_WINDOW_HOURS = 24

# 「管理员」这个名字只属于主人一个人。别人的群名片叫这个，就标成「冒充管理员」
_ADMIN_TITLES = {"管理员", "管理员本人", "群主"}  # 只认这几个，别把正常昵称也误伤

# 群成员的真实职位（群主/群管）：(群号, QQ号) -> (role, 查到的时刻)
_ROLES: dict[tuple[str, str], tuple[str, float]] = {}
_ROLE_TTL = 30 * 60

# 同一个会话同一时间只跑一轮：否则连发几条会同时开两口，回复叠在一起像轰炸
_TURN_LOCKS: dict[str, "asyncio.Lock"] = {}


def turn_lock(conv_id: str) -> "asyncio.Lock":
    lock = _TURN_LOCKS.get(conv_id)
    if lock is None:
        lock = asyncio.Lock()
        _TURN_LOCKS[conv_id] = lock
    return lock


def history_key(cfg, conv_id: str) -> str:
    """「刚才聊了什么」按人存，不按会话存。

    同一个人的两个号私聊（一张卡挂两个号）共用一段上下文 ——
    不然你在 A 号那边说完，去 B 号那边她就当没听过。群聊各管各的。
    """
    if not conv_id.startswith("private:"):
        return conv_id
    uid = norm_id(conv_id.split(":", 1)[-1])
    card = _card_of(getattr(cfg, "admins", None) or cfg, uid) if cfg is not None else None
    if card is None:
        return conv_id
    return f"person:{card['name']}"


def recent_turns(conv_id: str) -> list[dict]:
    return list(_HISTORY.get(conv_id, []))


def remember_turn(conv_id: str, role: str, text: str) -> None:
    """记一条最近对话，只留一点，别把提示词撑爆。"""
    text = str(text or "").strip()
    if not text:
        return
    rows = _HISTORY.setdefault(conv_id, [])
    rows.append({"role": role, "content": text})
    del rows[:-_HISTORY_KEEP]
    total = sum(len(r["content"]) for r in rows)
    while total > _HISTORY_CHARS and len(rows) > 2:
        total -= len(rows[0]["content"])
        rows.pop(0)


def forget_turns(conv_id: str) -> None:
    _HISTORY.pop(conv_id, None)


def _seed_line(conv_id: str, row: dict, owner) -> str:
    """把库里的一行还原成她平时看到的样子（群里带群号和 QQ 号，私聊带私聊）。"""
    text = str(row.get("text") or "").strip()
    if not text:
        return ""
    uid = norm_id(row.get("sender_id") or "")
    name = str(row.get("sender_name") or "").strip()
    is_group = conv_id.startswith("group:")
    cards = _admin_cards(owner)
    card = next((c for c in cards if uid and uid in c["ids"]), None)
    if card:
        label = _card_label(owner, card, uid, is_group)
    else:
        label = f"{name} #{uid}" if name and uid else (name or uid or "某人")
    if is_group:
        tag = f"[群{norm_id(conv_id.split(':', 1)[-1])}]"
    else:
        tag = "[私聊]"
    return f"{tag} {label}: {text}"


def history_convs(cfg, conv_id: str) -> list[str]:
    """这段对话要从哪几个会话里取「刚才聊了什么」。

    私聊：同一个人在称呼卡上的几个号合并成一段（在 A 号说完，换 B 号她也知道）；
    群聊：只取这个群自己 —— 群里的语境不能串到私聊，反过来也一样。
    """
    if not conv_id.startswith("private:"):
        return [conv_id]
    uid = norm_id(conv_id.split(":", 1)[-1])
    card = _card_of(getattr(cfg, "admins", None), uid) if cfg is not None else None
    if card is None:
        return [conv_id]
    return [f"private:{u}" for u in card["ids"]] or [conv_id]


def load_history(ctx, conv_id: str, skip_ids: set[str], owner: str = "",
                 convs: list[str] | None = None) -> list[dict]:
    """直接从库里拼「刚才聊了什么」——每轮都读，不靠内存。

    内存那份只在进程活着时有用：一重启、或者换个会话唤醒，模型就只看见那个会话自己
    的几条。库里是逐条记录（群/私聊都在），按人合并才是真的同步。
    """
    convs = list(convs) if convs else history_convs(getattr(ctx, "cfg", None), conv_id)
    rows: list[dict] = []
    for conv in convs:
        try:
            rows.extend(ctx.store.message_list(conv, limit=_HISTORY_KEEP * 2) or [])
        except Exception as exc:
            ctx.log.warning("读刚才的话失败（%s）：%s", conv, exc)
    if not rows:
        return []
    # 去掉当前这一批（马上要作为 user 发出去，别重复），按时间排，再按 id 去重
    seen: set[str] = set()
    kept: list[dict] = []
    for row in rows:
        mid = str(row.get("message_id") or "")
        if mid and mid in skip_ids:
            continue
        key = str(row.get("id") or mid or f"{row.get('conv_id')}:{row.get('ts')}")
        if key in seen:
            continue
        seen.add(key)
        kept.append(row)
    if not kept:
        return []
    kept.sort(key=lambda r: (int(r.get("ts") or 0), int(r.get("id") or 0)))
    newest = int(kept[-1].get("ts") or 0)
    if newest and (time.time() * 1000 - newest) > _HISTORY_WINDOW_HOURS * 3600 * 1000:
        return []

    out: list[dict] = []
    total = 0
    for row in reversed(kept[-_HISTORY_KEEP:]):
        conv = str(row.get("conv_id") or conv_id)
        if str(row.get("direction")) == "out":
            text = str(row.get("text") or "").strip()
            if text:
                out.append({"role": "assistant", "content": text})
                total += len(text)
        else:
            text = _seed_line(conv, row, owner)
            if text:
                out.append({"role": "user", "content": text})
                total += len(text)
        if total >= _HISTORY_CHARS or len(out) >= _HISTORY_KEEP:
            break
    out.reverse()
    return out


def seed_history(ctx, conv_id: str, skip_ids: set[str], owner: str = "",
                 siblings: list[str] | None = None) -> None:
    """把库里最近的几句接回内存（跟平时同一个上限：20 条 / 1800 字）。

    现在每轮都直接 load_history 从库里取，这里只留给「要先塞进内存」的场合
    （别的插件读 _HISTORY、或者老代码路径）。规矩跟 load_history 完全一致：
    窗口、条数、字数都那边管，这里不另立一套。
    """
    hkey = history_key(getattr(ctx, "cfg", None), conv_id)
    if _HISTORY.get(hkey):
        return
    convs = None
    if siblings:
        convs = [c for c in siblings if c] or None
    items = load_history(ctx, conv_id, skip_ids, owner, convs=convs)
    for item in items:
        remember_turn(hkey, item["role"], item["content"])
    if items:
        ctx.log.info("接回刚才的 %s 条（%s）", len(items), hkey)


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


def _as_card(card) -> dict:
    """一张卡统一成 dict（Admin 对象 / dict / 纯号码都认）。"""
    if isinstance(card, dict):
        call = card.get("call") or {}
        return {
            "name": str(card.get("name") or ""),
            "ids": [str(i) for i in (card.get("ids") or [])],
            "private": str(call.get("private") or ""),
            "group": str(call.get("group") or ""),
            "by_group": {str(k): str(v) for k, v in (call.get("by_group") or {}).items()},
        }
    return {
        "name": str(getattr(card, "name", "") or ""),
        "ids": [str(i) for i in (getattr(card, "ids", None) or [])],
        "private": str(getattr(card, "private_call", "") or ""),
        "group": str(getattr(card, "group_call", "") or ""),
        "by_group": {
            str(k): str(v) for k, v in (getattr(card, "by_group", None) or {}).items()
        },
    }


def _admin_cards(value) -> list[dict]:
    """把 cfg / 称呼卡列表 / 号码列表 / 字符串统一成「称呼卡」列表。

    - Config 或卡列表：直接用（一卡一人，可挂多个号）
    - 只有号码：造一张叫「管理员」的卡，老配置照旧能用
    """
    cards = getattr(value, "admins", None)
    if not cards and isinstance(value, (list, tuple)) and value:
        first = value[0]
        if isinstance(first, dict) or hasattr(first, "ids"):
            cards = list(value)
    if cards:
        return [_as_card(card) for card in cards]
    # 用户明确把卡删光了（admins: []）→ 就是没有卡，不再拿管理员 QQ 兜一张
    if getattr(value, "admins_explicit", False):
        return []
    ids = _owner_ids(getattr(value, "owner_ids", None) or getattr(value, "owner_qq", value))
    if not ids:
        return []
    return [{"name": "管理员", "ids": ids, "private": "", "group": "", "by_group": {}}]


def _card_of(value, uid: str) -> dict | None:
    """这个 QQ 号属于哪张卡（单人多号时几个号都命中同一张）。"""
    uid = norm_id(uid)
    if not uid:
        return None
    for card in _admin_cards(value):
        if uid in card["ids"]:
            return card
    return None


def _admin_default_name(value, card: dict) -> str:
    """卡名留空时的默认叫法：一张卡叫「管理员」，多张按位置叫 管理员A/管理员B/…"""
    cards = _admin_cards(value)
    try:
        idx = cards.index(card)
    except ValueError:
        idx = 0
    if len(cards) <= 1:
        return "管理员"
    return "管理员" + chr(ord("A") + min(idx, 25))


def _card_label(value, card: dict, uid: str, is_group: bool) -> str:
    """卡 → 显示标签：卡名（空则 管理员/管理员A）；群里单卡只写「管理员」。"""
    cards = _admin_cards(value)
    name = str(card.get("name") or "").strip()
    explicit = bool(name)
    if not explicit:
        name = _admin_default_name(value, card)
    # 一律带号码：群里也能看出是哪个号说的（不再有「光秃秃的 管理员」）
    return f"{name} #{uid}" if uid else name


def _admin_label(value, msg: Message) -> str:
    """管理员的标签：`卡名 #号码`；卡名留空时按 管理员 / 管理员A、B、C 排。"""
    card = _card_of(value, msg.sender_id)
    if card is None:
        return ""
    return _card_label(value, card, norm_id(msg.sender_id), msg.kind == "group")


def _split_calls(raw) -> list[str]:
    """一个字段里可以写多个称呼，逗号/顿号/斜杠隔开。"""
    return [p.strip() for p in re.split(r"[,，、;；/]+", str(raw or "")) if p.strip()]


def call_hint(ctx, msg: Message) -> str:
    """称呼偏好：只说当前场景那一句，没配就一个字都不占。

    称呼词本身在人设卡里（爸/老爸/提款机那一套），这里只是「他希望优先怎么叫」。
    """
    card = _card_of(getattr(ctx, "cfg", None), msg.sender_id)
    if card is None:
        return ""
    if msg.kind == "group":
        gid = norm_id(msg.conv_id.split(":", 1)[-1])
        want = (card["by_group"].get(gid) or card["group"]).strip()
    else:
        want = (card["private"] or "").strip()
    items = _split_calls(want)
    if not items:
        return ""
    who = str(card.get("name") or "").strip()
    if not who:
        who = _admin_default_name(getattr(ctx, "cfg", None), card)
    if len(items) == 1:
        body = f"希望你叫他「{items[0]}」"
    else:
        pool = "".join(f"「{w}」" for w in items[:4])
        body = f"希望你叫他{pool}，叫哪个都行"
    return f"\n\n【叫法】{who} {body}；不想这么叫也没关系。"


def _owner_ids(value) -> list[str]:
    """管理员 QQ 列表。传进来的可能是字符串、"a,b"、列表，统一成去重的号码列表。"""
    if isinstance(value, (list, tuple, set)):
        parts = [str(v) for v in value]
    else:
        parts = [p for p in re.split(r"[,，;；\s]+", str(value or ""))]
    out: list[str] = []
    for part in parts:
        uid = norm_id(part)
        if uid and uid not in out:
            out.append(uid)
    return out


def member_role(msg: Message) -> str:
    """他在那个群里的真实职位：owner / admin / member（查不到就当普通群友）。"""
    gid = norm_id(msg.conv_id.split(":", 1)[-1])
    uid = norm_id(msg.sender_id)
    hit = _ROLES.get((gid, uid))
    if not hit:
        return ""
    role, ts = hit
    if time.time() - ts > _ROLE_TTL:
        return ""
    return role


async def ensure_member_role(ctx, msg: Message) -> None:
    """先把「这个人是不是群主/群管」查出来缓存住，渲染时就不用等网络了。

    一个群里有多个管理员是常态（有的群主还不是主人）。只查没见过的，
    查过就缓存半小时；查失败记一次短缓存，别把接口打爆。
    """
    if msg.kind != "group":
        return
    gid = norm_id(msg.conv_id.split(":", 1)[-1])
    uid = norm_id(msg.sender_id)
    if not gid or not uid:
        return
    hit = _ROLES.get((gid, uid))
    if hit and time.time() - hit[1] <= _ROLE_TTL:
        return
    client = ctx.services.get("onebot") if getattr(ctx, "services", None) else None
    if client is None:
        return
    try:
        data = await client.call(
            "get_group_member_info", {"group_id": int(gid), "user_id": int(uid)}
        )
    except Exception as exc:
        ctx.log.debug("查群成员职位失败（%s/%s）：%s", gid, uid, exc)
        _ROLES[(gid, uid)] = ("", time.time() - _ROLE_TTL + 60)  # 60 秒后再试
        return
    role = str((data or {}).get("role") or "").strip().lower()
    _ROLES[(gid, uid)] = (role, time.time())


def speaker_label(msg: Message, owner) -> str:
    """说话人标签：卡名 / 群主 / 群管 / 名字 #QQ号。

    - 称呼卡里的号 → `管理员A #号码`（一卡多号时分得清是哪个号、但知道是同一人）
    - 真职位另外标：群主、群管 —— 一个群里有好几个管理员也不会混
    - 别人把名字改成「管理员」的，标成「冒充管理员 #号码」
    """
    label = _admin_label(owner, msg)
    if label:
        return label
    uid = norm_id(msg.sender_id)
    name = (msg.sender_name or "").strip()
    if name in _ADMIN_TITLES:
        name = f"冒充{name}"
    role = member_role(msg)
    if role == "owner":
        name = f"群主 {name}".strip()
    elif role == "admin":
        name = f"群管 {name}".strip()
    if not uid:
        return name or "某人"
    return f"{name} #{uid}" if name else uid


def conv_tag(msg: Message) -> str:
    """每条消息前面挂一个「在哪」：群里挂群号，私聊就写私聊。"""
    if msg.kind == "group":
        gid = norm_id(msg.conv_id.split(":", 1)[-1])
        return f"[群{gid}]" if gid else "[群]"
    return "[私聊]"


def group_format_hint() -> str:
    """格式说明：`[群123456] 小明(123456): 在吗` 怎么读。

    少了这句，她会把群名片当话题接（"某人的群名片？不然用啥"）。
    """
    return (
        "\n\n【怎么看消息】每条都长这样：「[群123456] 小明 #123456: 在吗」。"
        "方括号里是群号（私聊会写「[私聊]」），冒号前面是说话的人，"
        "「#」后面那串数字是他的 QQ 号 —— 昵称随时会改，认号码才认得住是同一个人。"
        "名字前面的「群主」「群管」是他在这个群里的真实职位（一个群可能有好几个管理员，"
        "都按这个标）；标着「管理员」的是爸，跟群职位无关。"
        "自己人不止一个时，会分别叫「管理员A」「管理员B」；同一个人挂了两个号，就卡名相同、"
        "「#号码」不同 —— 那是一个人的两个号，不是两个人。"
        "别人把名字改成「管理员」的会被标成「冒充管理员 #号码」，那不是他。"
    )


def render_batch(batch: list[Message], owner: str = "") -> str:
    """把合流窗口里的几条消息拼成一段上下文（一行一条，带群号/私聊和 QQ 号）。"""
    return "\n".join(f"{conv_tag(m)} {speaker_label(m, owner)}: {m.text}" for m in batch)


async def setup(ctx) -> None:
    merger = Merger(ctx.cfg.merge_window_ms, lambda batch: handle_batch(ctx, batch),
                    max_wait_ms=int(getattr(ctx.cfg, "merge_max_wait_ms", 0) or 0))
    ctx.services["merger"] = merger
    ctx.on("message.received", lambda msg: on_message(ctx, merger, msg), priority=10)
    await _setup_tools(ctx)
    asyncio.create_task(_pull_groups_when_ready(ctx))
    ctx.services["catch_up"] = asyncio.create_task(_catch_up(ctx))
    ctx.log.info("海螺就绪（合流窗口 %sms，表情包 %s）",
                 ctx.cfg.merge_window_ms,
                 "接上" if ctx.services.get("stickers") is not None else "没接")


CATCH_UP_MIN_MS = 60 * 1000        # 一分钟内的可能还在合流窗口里，让正常流程去回


async def _catch_up(ctx) -> None:
    """重启/断线期间没回成的那几句，补一次。

    合流窗口里的批次在进程退出时会丢（停服、重启、一键关闭都算），
    不补的话主人在那几分钟里说的话就永远没有回音。
    只补私聊：群里没 @ 她的消息不该因为她重启而冒出来回一句。
    """
    if not getattr(getattr(ctx, "cfg", None), "catch_up_enabled", True):
        return
    await asyncio.sleep(8)
    minutes = max(1, int(getattr(getattr(ctx, "cfg", None), "catch_up_minutes", 30) or 30))
    window_ms = minutes * 60 * 1000
    now = time.time() * 1000
    try:
        convs = ctx.store.message_convs(limit=20)
    except Exception as exc:
        ctx.log.warning("补回消息：读会话失败 %s", exc)
        return
    for row in convs:
        conv = str(row.get("conv_id") or "")
        last_ts = int(row.get("last_ts") or 0)
        if not conv.startswith("private:") or not last_ts:
            continue
        if not (CATCH_UP_MIN_MS < now - last_ts <= window_ms):
            continue
        try:
            recent = ctx.store.message_list(conv, limit=20)   # 新的在前
        except Exception:
            continue
        pending = []
        for item in recent:
            if item.get("direction") != "in":
                break          # 最后一条是她的回复，就不用补
            pending.append(item)
        pending.reverse()
        msgs = [
            Message(
                conv_id=conv,
                kind="private",
                sender_id=str(item.get("sender_id") or ""),
                sender_name=str(item.get("sender_name") or ""),
                message_id=str(item.get("message_id") or ""),
                segments=make_text(str(item.get("text") or "")),
                ts=int(item.get("ts") or 0),
            )
            for item in pending
            if str(item.get("text") or "").strip()
        ]
        if not msgs:
            continue
        # 她关机前主人已经道过晚安了，就别重启后追一句
        if is_closing(ctx, msgs[-1].text):
            ctx.log.info("补回跳过（收尾话）：%s", conv)
            continue
        ctx.log.info("补回没回的消息：%s（%d 条）", conv, len(msgs))
        try:
            await handle_batch(ctx, msgs)
        except Exception as exc:
            ctx.log.warning("补回失败 %s：%s", conv, exc)


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
        from core.mcp import McpPool

        pool = McpPool(servers)
        await pool.start()
        ctx.services["mcppool"] = pool
        total = sum(len(srv._tools) for srv in pool.servers.values())
        ctx.log.info("工具接好了：%s 个（%s）", total, "、".join(pool.servers))
    except Exception as exc:
        ctx.log.warning("工具接入失败：%s", exc)


async def teardown(ctx) -> None:
    task = ctx.services.pop("catch_up", None)
    if task is not None:
        task.cancel()
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
        conv,
        {
            "last_reply": 0.0,
            "last_active": 0.0,
            "asleep": False,
            "hour": "",
            "hour_count": 0,
            "consecutive": 0,
        },
    )


def asleep(ctx, conv: str) -> bool:
    """她是不是睡着了：群里安静超过 sleep_after_sec 就算睡。"""
    st = ctx.cfg.strategy or {}
    quiet = int(st.get("sleep_after_sec") or 0)
    if quiet <= 0:
        return False
    state = _st(conv)
    last = float(state.get("last_active") or 0)
    if not last:
        return False
    return (time.time() - last) > quiet


def wake_gap_blocked(ctx, conv: str) -> bool:
    """两次主动开口之间要隔够 wake_gap_sec。"""
    st = ctx.cfg.strategy or {}
    gap = int(st.get("wake_gap_sec") or 0)
    if gap <= 0:
        return False
    state = _st(conv)
    last = float(state.get("last_reply") or 0)
    return bool(last) and (time.time() - last) < gap


def in_quiet_hours(st: dict) -> bool:
    """免打扰时段：只回 @，不主动开口。跨零点也能算（23:00–07:30）。"""
    start = str(st.get("quiet_start") or "").strip()
    end = str(st.get("quiet_end") or "").strip()
    if not (start and end):
        return False
    try:
        sh, sm = [int(x) for x in start.split(":")]
        eh, em = [int(x) for x in end.split(":")]
    except ValueError:
        return False
    now = time.localtime()
    cur = now.tm_hour * 60 + now.tm_min
    a, b = sh * 60 + sm, eh * 60 + em
    if a == b:
        return False
    if a < b:
        return a <= cur < b
    return cur >= a or cur < b


def too_short(msg: Message, st: dict) -> bool:
    """群里那种「嗯」「哦」不值得接；@ 了例外。"""
    limit = int(st.get("min_chars") or 0)
    if limit <= 0 or msg.kind == "private" or msg.at_me:
        return False
    return len(str(msg.text or "").strip()) < limit


def should_reply(ctx, msg: Message) -> bool:
    st = ctx.cfg.strategy or {}
    if st.get("participate") == "listen_only":
        return False
    trigger = st.get("trigger", "at_only")
    if msg.kind == "private":
        return True
    if asleep(ctx, msg.conv_id) and not msg.at_me:
        # 睡着了：群里只在被 @ 时醒，别的一概不理
        name = str((ctx.cfg.raw or {}).get("bot_name") or "").strip()
        if not (name and name in msg.text):
            return False
    if too_short(msg, st):
        return False
    if in_quiet_hours(st) and not msg.at_me:
        return False
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
    if wake_gap_blocked(ctx, conv):
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
    state["last_active"] = time.time()
    state["asleep"] = False
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


async def talk(
    ctx,
    messages: list[dict],
    scope: str,
    extra_tools: list[dict] | None = None,
    extra_call=None,
) -> tuple[str, dict[str, int]]:
    """有工具就走工具循环，没有就一句话返回。

    extra_tools / extra_call 是「只在这一次对话里给她的工具」——
    遥控发群就靠它：工具结果进模型的上下文，成没成由她自己说。
    """
    backend = ctx.services["backend"]
    pool = ctx.services.get("mcppool")
    toolbox = ctx.services.get("tools")
    tools = list(pool.tools_for(scope)) if pool is not None else []
    if toolbox is not None:
        tools.extend(toolbox.specs())
    if extra_tools:
        tools.extend(extra_tools)
    if not tools:
        return await backend.chat(messages)

    rounds = int(((ctx.cfg.raw or {}).get("tools") or {}).get("max_rounds") or 6)
    total = {"prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0}
    msgs = list(messages)

    for round_no in range(max(1, rounds)):
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
            payload = args if isinstance(args, dict) else {}
            result = None
            if toolbox is not None:
                result = await toolbox.call(name, payload)
            if result is None and extra_call is not None:
                result = await extra_call(name, payload)
            if result is None:
                if pool is None:
                    result = f"没有这个工具：{name}"
                else:
                    result = await pool.call(name, payload)
            msgs.append(
                {"role": "tool", "tool_call_id": str(call.get("id") or ""), "content": result}
            )

    # 轮数用完，逼她说句人话（不带工具）
    # 2026-11-08：她发语音那次就是走到这儿 —— 工具轮数吃满（4 轮），
    # 最后一次不带工具的请求模型返回空，于是「模型返回空，不发送」，人就没声了。
    # 现在：先正常逼一次，还是空的就催一句再要一次。
    message, usage = await backend.chat_raw(msgs)
    for k in total:
        total[k] += int(usage.get(k) or 0)
    text = str(message.get("content") or "").strip()
    if text:
        return text, total

    ctx.log.info("轮数用完了她还是没说话，催一次（tok=%s）", total.get("completion_tokens"))
    nudge = list(msgs)
    nudge.append({"role": "user", "content": "（别调工具了，把你要说的话直接说出来）"})
    message, usage = await backend.chat_raw(nudge)
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
主人私聊你、让你去某个群说点什么时（哪怕那个群一个人都没说话），用 send_group_message 工具：
group 写主人说的群名或群号，text 是要发的话。
发完工具会告诉你结果，你再按自己的口气回主人一句——成了就顺嘴说一声，没成就告诉他为什么。
如果主人只说了"去群里说一句"，没指哪个群，就问他去哪个群，不要猜。
不要在没有被要求的时候主动往群里发东西。"""

SEND_TOOL = {
    "type": "function",
    "function": {
        "name": "send_group_message",
        "description": (
            "往一个 QQ 群里发消息（只有主人私聊让你发的时候才用）。"
            "group 写群名或群号，text 是要发的正文。返回发没发成和原因。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "group": {"type": "string", "description": "群名或群号"},
                "text": {"type": "string", "description": "要发到群里的正文"},
            },
            "required": ["group", "text"],
        },
    },
}


async def call_send_tool(ctx, args: dict) -> str:
    """send_group_message 的实现：结果直接给模型，让她自己决定怎么说。"""
    target = str((args or {}).get("group") or "").strip()
    text = str((args or {}).get("text") or "").strip()
    if not target or not text:
        return "没发：群名或正文是空的。"
    ok, detail = await send_to_group(ctx, target, text)
    return detail if ok else f"没发成：{detail}"

SEND_RE = re.compile(r"<SEND\s+([^<>\n]{1,40})>(.*?)</SEND>", re.S)
_GROUPS: dict[str, str] = {}  # 群号 -> 群名


def is_owner_private(msg: Message, cfg) -> bool:
    """是不是「管理员私聊」—— 只有这一种会话能下管理命令（管理员可能有多个号）。"""
    if msg.kind != "private":
        return False
    return norm_id(msg.sender_id) in _owner_ids(getattr(cfg, "owner_ids", None) or getattr(cfg, "owner_qq", ""))


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


def cap_lines(parts: list[str], limit: int) -> list[str]:
    """一轮最多几条：超出的文字行并到最后一条里，表情包行不算也不吞。

    模型很容易越写越多（人格卡还写着"优先拆成多条"），真的按它写的发，
    主人那边就是被连着刷屏。这里兜个底：前几条照发，后面的合成一条。
    """
    if limit <= 0 or not parts:
        return parts
    out: list[list[str]] = []      # [kind, text]，kind = text / sticker
    texts = 0
    for part in parts:
        if _STICKER_LINE.match(part):
            out.append(["sticker", part])
            continue
        if texts >= limit:
            # 已经够了：并进最后一个文字行
            for item in reversed(out):
                if item[0] == "text":
                    item[1] = (item[1] + " " + part).strip()
                    break
            else:
                out.append(["text", part])
                texts += 1
            continue
        out.append(["text", part])
        texts += 1
    return [item[1] for item in out]


# ---------- 表情包：本地库没有就去大厅拿 ----------

HALL_BASE = "http://127.0.0.1:7802/"
# 模型写的是描述（"猫猫-无语"），大厅按标准标签存，先把描述翻成标签
_HALL_TAG_HINTS: dict[str, tuple[str, ...]] = {
    "happy": ("开心", "笑", "哈哈", "高兴", "乐", "得意"),
    "angry": ("生气", "怒", "气死", "炸", "翻脸"),
    "sad": ("难过", "哭", "委屈", "伤心", "破防"),
    "shy": ("害羞", "脸红", "羞", "不好意思"),
    "confuse": ("疑惑", "问号", "啥", "问"),
    "speechless": ("无语", "沉默", "白眼", "服了", "毁灭"),
    "neutral": ("平淡", "普通"),
}


def hall_tag_of(hint: str) -> str:
    """把「【表情包：猫猫-无语】」翻成大厅的标签（speechless 之类）。"""
    text = str(hint or "")
    for tag, words in _HALL_TAG_HINTS.items():
        if any(word in text for word in words):
            return tag
    return ""


def _hall_json(path: str, timeout: float = 3.0) -> dict:
    with urllib.request.urlopen(HALL_BASE + path, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _hall_bytes(image_id: int, timeout: float = 8.0) -> bytes:
    with urllib.request.urlopen(f"{HALL_BASE}api/img?id={image_id}", timeout=timeout) as resp:
        return resp.read()


def _hall_cache(ctx, image_id: int, name: str) -> Path:
    folder = Path(ctx.cfg.data_dir) / "hall"
    folder.mkdir(parents=True, exist_ok=True)
    suffix = Path(str(name or "")).suffix or ".webp"
    return folder / f"{image_id}{suffix}"


async def send_hall_sticker(ctx, conv_id: str, hint: str = "") -> bool:
    """本地表情库没有就去「表情包收录库」拿一张：按标签挑，挑不到就随机。

    这一条是给主人填坑用的：本地库是空的，她只好拿 QQ 自带表情凑数。
    """
    try:
        tag = hall_tag_of(hint)
        items = []
        for query in (
            "api/images?size=60" + (f"&tag={tag}" if tag else ""),
            "api/images?size=60",     # 标签对不上就退回全部（大厅里大部分还没打标）
        ):
            data = await asyncio.to_thread(_hall_json, query)
            items = [it for it in (data.get("items") or []) if it.get("file_path")]
            if items:
                break
        if not items:
            return False
        pick = random.choice(items)
        image_id = int(pick["id"])
        target = _hall_cache(ctx, image_id, str(pick.get("file_name") or ""))
        if not target.exists():
            raw = await asyncio.to_thread(_hall_bytes, image_id)
            if not raw:
                return False
            target.write_bytes(raw)
        await ctx.send(conv_id, [Segment("image", {"file": str(target)})])
        ctx.log.info("发了一张大厅表情：#%s %s", image_id, "/".join(pick.get("tags") or []) or "随机")
        return True
    except Exception as exc:
        ctx.log.debug("大厅表情没拿到：%s", exc)
        return False


def split_audio(text: str) -> list[tuple[str, str]]:
    """把回复按 <audio src="..."> 切成 (text, 文本) / (voice, 音频路径)。"""
    text = str(text or "")
    items: list[tuple[str, str]] = []
    pos = 0
    for match in _AUDIO_TAG.finditer(text):
        head = text[pos : match.start()]
        if head.strip():
            items.append(("text", head))
        path = next((g for g in match.groups() if g), "").strip()
        if path:
            items.append(("voice", path))
        pos = match.end()
    tail = text[pos:]
    if tail.strip():
        items.append(("text", tail))
    return items or [("text", text)]


async def send_voice(ctx, conv_id: str, path: str) -> bool:
    """把本地音频文件发成一条 QQ 语音；文件不在就跳过，绝不把路径当话说出去。"""
    raw = str(path or "").strip().strip('"').strip("'").replace("\\\\", "\\")
    target = Path(raw)
    if not target.is_absolute():
        target = Path(ctx.cfg.data_dir) / raw
    try:
        if not target.is_file():
            ctx.log.warning("语音文件不存在，跳过：%s", target)
            return False
        await ctx.send(conv_id, [Segment("record", {"file": str(target)})])
        ctx.log.info("发了一条语音：%s", target.name)
        return True
    except Exception as exc:
        ctx.log.warning("语音发送失败 %s：%s", target, exc)
        return False


async def send_lines(ctx, conv_id: str, text: str) -> None:
    """一句一条：模型吐出来的每一行单独发一条，像真人连着打字。

    表情包：模型写「【表情包：名字】」的那一行，换成真的发一张图；
    本地库对不上就去大厅挑一张；还是没有就跳过——
    绝不把方括号那句当成文字发出去（那才叫"发送异常"）。

    语音：模型写「<audio src="...">」的那一段（或直接甩一行本地音频路径），
    换成真的发一条语音——voice_tts 只给路径，海螺得自己把它变成 record 段。
    """
    stickers = ctx.services.get("stickers") if getattr(ctx, "services", None) else None
    limit = int(getattr(getattr(ctx, "cfg", None), "reply_max_lines", 4) or 0)
    sent_any = False
    for kind, chunk in split_audio(text):
        if kind == "voice":
            if await send_voice(ctx, conv_id, chunk):
                sent_any = True
            continue
        parts = [line.strip() for line in re.split(r"\n+", chunk) if line.strip()]
        if not parts:
            continue
        parts = cap_lines(parts, limit)
        for part in parts:
            if _AUDIO_PATH_LINE.match(part) and await send_voice(ctx, conv_id, part):
                sent_any = True
                continue
            hit = _STICKER_LINE.match(part)
            if hit:
                hint = hit.group(1).strip()
                ok = False
                # 先看本地库有没有对得上的；对不上就去大厅挑（本地库常常是空的）
                store = getattr(ctx, "store", None)
                local = None
                if stickers is not None and store is not None:
                    try:
                        local = store.sticker_find(hint)
                    except Exception:
                        local = None
                if stickers is not None and (local is not None or store is None):
                    try:
                        ok = await stickers.pick_and_send(conv_id, hint)
                    except Exception as exc:
                        ctx.log.warning("发表情失败（%s）：%s", hint, exc)
                        ok = False
                if not ok:
                    ok = await send_hall_sticker(ctx, conv_id, hint)
                if not ok and stickers is not None:
                    try:
                        ok = await stickers.pick_and_send(conv_id, "")
                    except Exception:
                        ok = False
                if not ok:
                    ctx.log.info("表情没发出去，跳过这一行：「%s」", hint)
                continue
            clean = _STICKER_ANY.sub("", part).strip()
            if not clean:
                continue
            if sent_any:
                # 每句之间歇一下，像真人一字一字敲出来（长句歇得久，但有上限）。
                # 停顿在面板「设置」里可调：基础 0.70s、每字 +20ms、上限 2.20s。
                base = getattr(getattr(ctx, "cfg", None), "reply_gap_base_ms", 700) / 1000.0
                cap = getattr(getattr(ctx, "cfg", None), "reply_gap_max_ms", 2200) / 1000.0
                per = getattr(getattr(ctx, "cfg", None), "reply_gap_char_ms", 20) / 1000.0
                gap = base + len(clean) * per
                if cap > 0:
                    gap = min(gap, cap)
                if gap > 0:
                    await asyncio.sleep(gap)
            await ctx.send(conv_id, to_segments(clean))
            sent_any = True


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
        await send_lines(ctx, conv, text)
    except Exception as exc:
        ctx.log.error("遥控发送失败 %s：%s", conv, exc)
        return False, f"发到「{group_name(gid)}」失败：{exc}"
    try:
        ctx.store.message_add(conv, "out", text, plugin_id=ctx.id)
    except Exception:
        pass
    _ = asyncio.create_task(remember_own(ctx, conv, text, f"remote-{int(time.time() * 1000)}"))
    ctx.log.info("遥控发群 %s：%s", conv, text[:40])
    return True, f"已发到「{group_name(gid)}」"


async def run_remote(ctx, reply: str) -> str:
    """兜底：把回复里残留的 <SEND> 执行掉，标签本身不发给主人。

    回执不再拼回回复 —— 现在有 send_group_message 工具，成没成她自己说。
    这里只防她旧习惯漏出标签。
    """
    if "<SEND" not in reply:
        return reply
    for match in SEND_RE.finditer(reply):
        target = match.group(1).strip()
        text = match.group(2).strip()
        if not text:
            continue
        try:
            ok, detail = await send_to_group(ctx, target, text)
        except Exception as exc:
            ok, detail = False, f"发送出错：{exc}"
        ctx.log.info("遥控（标签兜底）%s：%s", "成功" if ok else "失败", detail)
    return SEND_RE.sub("", reply).strip()


# ── 主流程 ────────────────────────────────────────────────────


# ---------- 自动入库：消息进记忆库，跟她想不想得起来无关 ----------

def _card_who(ctx, sender_id: str) -> str:
    """这个人的卡名（记忆槽位用）；外人没有卡，返回空。"""
    card = _card_of(ctx.cfg, str(sender_id or ""))
    if not card:
        return ""
    return str(card.get("name") or "").strip() or _admin_default_name(ctx.cfg, card)


def _conv_key(conv_id: str, kind: str, who: str = "") -> str:
    """记忆库的槽位。

    - 群：`group:<群号>`——一个群一个槽
    - 私聊、有称呼卡：`private:card:<卡名>`——一张卡绑几个号，在记忆里还是同一个人
    - 私聊、没卡（外人）：`private:<QQ号>`——张三和李四绝不能拼成一条
    """
    tail = norm_id(str(conv_id or "").split(":", 1)[-1])
    if kind != "group":
        name = str(who or "").strip()
        if name:
            return f"private:card:{name}"
    if not tail:
        return str(conv_id or kind or "private")
    return f"{'group' if kind == 'group' else 'private'}:{tail}"


def _ms(ts) -> int:
    try:
        value = int(float(ts or 0))
    except Exception:
        return 0
    return value * 1000 if 0 < value < 10**11 else value


async def ring_ingest(
    ctx,
    *,
    conv_id: str,
    kind: str,
    speaker: str,
    content: str,
    message_id: str = "",
    at=0,
    note: str = "",
    who: str = "",
) -> None:
    """往记忆库写一条。这是「接盘层」：只在桥接这边调，不进她的工具清单。

    speaker 只认两个值：`user`（别人说的）/ `self`（她自己说的，记忆库里权重 ×3）。
    """
    if not getattr(ctx.cfg, "memory_ingest_enabled", True):
        return
    if not str(content or "").strip():
        return
    pool = ctx.services.get("mcppool") if getattr(ctx, "services", None) else None
    if pool is None:
        return
    payload = {
        "content": content,
        "speaker": "self" if speaker == "self" else "user",
        "source": "group" if kind == "group" else "private",
        "conv_key": _conv_key(conv_id, kind, who),
    }
    if message_id:
        payload["message_id"] = str(message_id)
    at_ms = _ms(at)
    if at_ms:
        payload["at"] = at_ms
    if note:
        payload["note"] = note
    try:
        reply = await pool.call_raw("ring", "ring_ingest", payload)
        ctx.log.debug("记忆入库：%s", str(reply)[:80])
    except Exception as exc:
        ctx.log.debug("记忆入库失败：%s", exc)


def _voice_text_of(msg) -> str:
    """这条消息如果是从语音转来的，取出转写正文（正文以 VOICE_MARK 开头）。"""
    text = str(getattr(msg, "text", "") or "")
    return text[len(VOICE_MARK):] if text.startswith(VOICE_MARK) else ""


async def remember_into_ring(ctx, msg: Message, heard: str = "") -> None:
    """把听到的话丢进记忆库（环形的心）。

    按「一个聊天一个槽位 + 一个发言方一个槽位」写，带 message_id 防重复投递。
    正文里保留「在哪 + 谁 + 号」，以后翻记忆认得出是同一个人换号、还是换了群。
    """
    if not str(msg.text or "").strip() and not heard:
        return  # 纯图 / 没转出来的语音：没有可记的文字
    display = speaker_label(msg, ctx.cfg)
    who = _card_who(ctx, msg.sender_id)
    if msg.kind == "group":
        gid = norm_id(msg.conv_id.split(":", 1)[-1])
        tag = f"[群{gid}]" if gid else "[群]"
    else:
        tag = "[私聊]"
    note = ""
    body = msg.text
    if heard:
        # 语音：正文带「语音转写·可能有错」标记 + 转写，note 再标注一次
        body = f"{VOICE_MARK.strip()} {heard}"
        note = "语音转写，可能不准"
    await ring_ingest(
        ctx,
        conv_id=msg.conv_id,
        kind=msg.kind,
        speaker="user",
        content=f"{tag} {display}：{body}",
        message_id=str(msg.message_id or ""),
        at=msg.ts,
        note=note,
        who=who,
    )


# ---------- 语音转写：跟「留影」借同一套 ASR（OpenAI 兼容 /audio/transcriptions） ----------

# 语音转出来的字不是原话：带个短标记，规则写在人设提示里（每轮一句，不逐条占地方）
VOICE_MARK = "[语音转写] "
VOICE_RULE = (
    "带「语音转写」的消息是机器听出来的，可能有错字：按上下文顺着理解（名字、常用词明显听错的，"
    "按对的理解），别把错字当原话；但顺字不能改意思，时间、数字、要做的事拿不准就问一句。"
)

_ASR_CACHE: dict = {"key": None, "cfg": None}
# 同时最多几条语音在转（单模型并发，不是多模型）
_ASR_SLOTS: dict = {"sem": None, "size": 0}


def _asr_semaphore(limit: int):
    size = max(1, min(4, int(limit or 2)))
    if _ASR_SLOTS["sem"] is None or _ASR_SLOTS["size"] != size:
        _ASR_SLOTS["sem"] = asyncio.Semaphore(size)
        _ASR_SLOTS["size"] = size
    return _ASR_SLOTS["sem"]


def _find_liuying_asr() -> dict:
    """从留影的 data/vision.json 里抄 asr 配置；找不到就空。"""
    roots = []
    try:
        roots.append(Path(__file__).resolve().parents[2].parent)  # 海螺的上一级 = 桌面\插件
    except Exception:
        pass
    for root in roots:
        for name in ("留影", "liuying-vision", "留影插件"):
            path = root / name / "data" / "vision.json"
            if path.is_file():
                try:
                    return dict(json.loads(path.read_text(encoding="utf-8-sig")).get("asr") or {})
                except Exception:
                    return {}
    return {}


def _asr_settings(ctx) -> dict | None:
    own = dict(getattr(ctx.cfg, "asr", None) or {})
    if own.get("enabled") is False:
        return None
    borrowed = _find_liuying_asr()
    base_url = str(own.get("base_url") or borrowed.get("baseUrl") or "").strip()
    model = str(own.get("model") or borrowed.get("model") or "").strip()
    api_key = str(own.get("api_key") or borrowed.get("apiKey") or "").strip()
    if not (base_url and model):
        return None
    ffmpeg = str(own.get("ffmpeg") or "").strip()
    if not ffmpeg:
        root = None
        try:
            root = Path(__file__).resolve().parents[2].parent
        except Exception:
            root = None
        for name in ("留影", "liuying-vision"):
            cand = (root / name / "bin" / "ffmpeg.exe") if root else None
            if cand and cand.is_file():
                ffmpeg = str(cand)
                break
    return {
        "base_url": base_url.rstrip("/"),
        "model": model,
        "api_key": api_key,
        "language": str(own.get("language") or borrowed.get("language") or ""),
        "ffmpeg": ffmpeg or "ffmpeg",
    }


def _voice_source(msg) -> tuple[str, str]:
    """返回 (kind, value)：kind 是 file / url / id。

    - `file`：本地就能读到的路径
    - `url`：QQ CDN 的下载链接
    - `id`：OneBot 给的文件名/缓存 id，要问 NapCat 拿（silk 就靠这条路转 mp3）
    """
    for seg in msg.segments:
        if seg.type != "record":
            continue
        path = str(seg.data.get("file") or "").strip()
        if path and Path(path).is_file():
            return "file", path
        url = str(seg.data.get("url") or "").strip()
        if url:
            return "url", url
        if path:
            return "id", path
    return "", ""


def _is_silk(raw: bytes) -> bool:
    return b"#!SILK" in raw[:16]  # 真实文件前面常有一个 0x02 字节


async def _record_via_onebot(ctx, file_id: str) -> bytes | None:
    """让 NapCat 把语音转成 mp3 再给我（它自带 silk 解码）。"""
    onebot = ctx.services.get("onebot") if getattr(ctx, "services", None) else None
    if onebot is None or not file_id:
        return None
    try:
        data = await onebot.call("get_record", {"file": file_id, "out_format": "mp3"})
    except Exception as exc:
        ctx.log.debug("get_record 失败：%s", exc)
        return None
    if not isinstance(data, dict):
        return None
    path = str(data.get("file") or "")
    url = str(data.get("url") or "")
    try:
        if path and Path(path).is_file():
            return Path(path).read_bytes()
        if url:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        return await resp.read()
    except Exception as exc:
        ctx.log.debug("取 get_record 结果失败：%s", exc)
    return None


def _node_bin(ctx) -> str:
    """找 node：插件里配的 → look 服务用的 → PATH → 常见位置。"""
    own = str((getattr(ctx.cfg, "asr", None) or {}).get("node") or "").strip()
    if own and Path(own).is_file():
        return own
    look = (getattr(ctx.cfg, "raw", None) or {}).get("look") or {}
    if isinstance(look, dict):
        node = str(look.get("node") or "").strip()
        if node and Path(node).is_file():
            return node
    found = shutil.which("node")
    if found:
        return found
    for cand in (r"C:\Program Files\nodejs\node.exe",):
        if Path(cand).is_file():
            return cand
    return ""


async def _silk_to_wav(ctx, raw: bytes, ffmpeg: str) -> bytes | None:
    """silk → 16k 单声道 wav（ffmpeg 解不了 silk，用 vendor 里的 silk-wasm 解）。"""
    node = _node_bin(ctx)
    script = Path(__file__).resolve().parent / "tools" / "silk_decode.cjs"
    if not node or not script.is_file():
        ctx.log.debug("没有 node 或 silk 解码脚本，解不了 silk")
        return None
    tmp = Path(getattr(ctx.cfg, "data_dir", "data")) / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time() * 1000)
    src = tmp / f"silk-{stamp}.silk"
    pcm = tmp / f"silk-{stamp}.pcm"
    try:
        src.write_bytes(raw)
        proc = await asyncio.create_subprocess_exec(
            node, str(script), str(src), str(pcm),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=60)
        if proc.returncode != 0 or not pcm.is_file():
            return None
        data = pcm.read_bytes()
        if len(data) < 1024:
            return None
        # 套个标准 wav 头（16 位单声道 16k），省一次转码
        header = b"RIFF" + (36 + len(data)).to_bytes(4, "little") + b"WAVEfmt "
        header += (16).to_bytes(4, "little") + (1).to_bytes(2, "little") + (1).to_bytes(2, "little")
        header += (16000).to_bytes(4, "little") + (32000).to_bytes(4, "little")  # privacy-ok 采样率/字节率
        header += (2).to_bytes(2, "little") + (16).to_bytes(2, "little") + b"data"
        header += len(data).to_bytes(4, "little")
        return header + data
    except Exception as exc:
        ctx.log.debug("silk 转 wav 失败：%s", exc)
        return None
    finally:
        for f in (src, pcm):
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass


async def _to_audio(ctx, source: str, value: str, ffmpeg: str) -> tuple[bytes, str] | None:
    """取到能喂给 ASR 的音频，返回 (字节, 格式)。"""
    raw = b""
    if source == "file":
        try:
            raw = Path(value).read_bytes()
        except Exception:
            raw = b""
    elif source == "url":
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(value) as resp:
                    if resp.status != 200:
                        return None
                    raw = await resp.read()
        except Exception as exc:
            ctx.log.debug("语音下载失败：%s", exc)
            return None

    if not raw:
        return None

    # QQ 语音基本都是 silk：自己解（ffmpeg 不认）
    if _is_silk(raw):
        wav = await _silk_to_wav(ctx, raw, ffmpeg)
        return (wav, "audio/wav") if wav else None

    if raw[:3] == b"ID3" or (len(raw) > 1 and raw[0] == 0xFF and raw[1] in (0xFB, 0xF3, 0xF2)):
        return raw, "audio/mpeg"

    tmp = Path(getattr(ctx.cfg, "data_dir", "data")) / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    src = tmp / f"asr-{int(time.time() * 1000)}.bin"
    dst = src.with_suffix(".mp3")
    try:
        src.write_bytes(raw)
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(dst),  # privacy-ok 采样率
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=60)
        if dst.is_file() and dst.stat().st_size > 1024:
            return dst.read_bytes(), "audio/mpeg"
        return None
    except Exception as exc:
        ctx.log.debug("语音转码失败：%s", exc)
        return None
    finally:
        for f in (src, dst):
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass


async def transcribe_voice(ctx, msg) -> str:
    """语音 → 文字。失败一律返回空串：听不清就当没听见，不能拖累回话。"""
    source, value = _voice_source(msg)
    if not source:
        return ""
    settings = _asr_settings(ctx)
    if not settings:
        ctx.log.debug("语音转写没配（留影里也没读到 asr）")
        return ""
    try:
        limit = int((getattr(ctx.cfg, "asr", None) or {}).get("concurrency") or 2)
    except Exception:
        limit = 2
    sem = _asr_semaphore(limit)

    async def _ours() -> str:
        async with sem:
            return await _transcribe_remote(ctx, source, value, settings)

    ours = await _ours()
    ours = ours if isinstance(ours, str) else ""
    if ours:
        ctx.log.info("语音转写：%s", ours[:40])
    return ours




async def _transcribe_remote(ctx, source: str, value: str, settings: dict) -> str:
    """我们自己的那路：取音频 → POST ASR。"""
    got = await _to_audio(ctx, source, value, settings["ffmpeg"])
    if not got:
        return ""
    audio, mime = got
    ext = "wav" if mime.endswith("wav") else "mp3"
    try:
        form = aiohttp.FormData()
        form.add_field("file", audio, filename=f"audio.{ext}", content_type=mime)
        form.add_field("model", settings["model"])
        if settings["language"]:
            form.add_field("language", settings["language"])
        headers = {"Authorization": f"Bearer {settings['api_key']}"} if settings["api_key"] else {}
        timeout = aiohttp.ClientTimeout(total=90)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                settings["base_url"] + "/audio/transcriptions", data=form, headers=headers
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    ctx.log.warning("语音转写失败 HTTP %s：%s", resp.status, body[:120])
                    return ""
                data = await resp.json()
        text = str(data.get("text") or "").strip()
        if text:
            ctx.log.info("语音转写：%s", text[:40])
        return text[:500]
    except Exception as exc:
        ctx.log.warning("语音转写出错：%s", exc)
        return ""


async def remember_own(ctx, conv_id: str, text: str, turn_id: str = "", who: str = "") -> None:
    """她自己发出去的话也写进记忆库：speaker=self，记忆库里按 ×3 存活。"""
    clean = _AUDIO_TAG.sub("[语音]", _STICKER_ANY.sub("", str(text or ""))).strip()
    if not clean:
        return
    kind = "group" if str(conv_id).startswith("group:") else "private"
    if kind == "group":
        gid = norm_id(str(conv_id).split(":", 1)[-1])
        tag = f"[群{gid}]" if gid else "[群]"
    else:
        tag = "[私聊]"
    await ring_ingest(
        ctx,
        conv_id=conv_id,
        kind=kind,
        speaker="self",
        content=f"{tag} {clean}",
        message_id=f"out:{turn_id}" if turn_id else "",
        who=who,
    )


async def on_message(ctx, merger: Merger, msg: Message) -> None:
    if not allowed(ctx.cfg, msg.conv_id, msg.sender_id):
        ctx.log.debug("不在白名单，忽略：%s / %s", msg.conv_id, msg.sender_id)
        return None
    # 先把他这个群里的真实职位（群主/群管）查出来，后面渲染才标得对
    await ensure_member_role(ctx, msg)
    # 有人在说话：刷新「最近活跃」，睡着的话就算被叫醒
    _state = _st(msg.conv_id)
    was_asleep = bool(_state.get("asleep"))
    _state["last_active"] = time.time()
    _state["asleep"] = False
    if was_asleep:
        ctx.log.info("被消息叫醒：%s", msg.conv_id)

    # 纯图 / 纯表情也要让别的插件看见（收藏表情用），但不能进对话
    try:
        await ctx.bus.emit(
            "message.observed",
            {
                "conv_id": msg.conv_id,
                "kind": msg.kind,
                "sender_id": msg.sender_id,
                "sender_name": msg.sender_name,
                "text": msg.text,
                "ts": msg.ts,
                "segments": [{"type": seg.type, "data": dict(seg.data)} for seg in msg.segments],
            },
        )
    except Exception as exc:
        ctx.log.debug("message.observed 出错：%s", exc)

    if not msg.text:
        # 纯语音：试着转成文字；转不出来就当没听见（图片 / 表情还是走上面的 observed）
        heard = await transcribe_voice(ctx, msg) if any(seg.type == "record" for seg in msg.segments) else ""
        if not heard:
            return None
        msg = replace(
            msg,
            segments=[Segment("text", {"text": f"{VOICE_MARK}{heard}"})]
            + [seg for seg in msg.segments if seg.type != "text"],
        )

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

    # 每条听到的话顺手进记忆库：不占她的回复时间，失败了也不影响说话
    _ = asyncio.create_task(remember_into_ring(ctx, msg, heard=_voice_text_of(msg)))

    if not should_reply(ctx, msg):
        return None
    if rate_limited(ctx, msg.conv_id):
        ctx.log.info("频率闸拦下一条：%s", msg.conv_id)
        return None
    merger.feed(msg)
    return None


async def handle_batch(ctx, batch: list[Message]) -> None:
    """一句一批。同一个会话排队来，不并发——并发就会出现两段回复叠在一起。"""
    conv_id = batch[0].conv_id
    if should_stay_quiet(ctx, batch):
        ctx.log.info("收尾话，不回：%s（%s）", conv_id, batch[-1].brief(20))
        return
    async with turn_lock(conv_id):
        await run_turn(ctx, batch)


def should_stay_quiet(ctx, batch: list[Message]) -> bool:
    """这批要不要闭嘴：主人拿收尾话结束聊天时，不回。

    群里被点名了照样回——"晚安 @她"是打招呼，不是结束。
    """
    if not batch:
        return False
    last = batch[-1]
    if not is_closing(ctx, last.text):
        return False
    if str(last.conv_id).startswith("private:"):
        return True
    return not last.at_me


# 这些尾巴跟着收尾话一起说时，还算收尾话（"晚安啦""我睡了哈"）
_CLOSING_TAIL = "啦咯了哈呀哦噢喔呢吧嘛啊喽咯"


def _flat(text: str) -> str:
    """去掉标点、空格、表情，只留能比对的字。"""
    text = _STICKER_ANY.sub("", str(text or ""))
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", text).strip()


def closing_words(ctx) -> list[str]:
    words = getattr(getattr(ctx, "cfg", None), "closing_words", None)
    return [str(w) for w in words] if words else list(DEFAULT_CLOSING_WORDS)


def is_closing(ctx, text: str) -> bool:
    """这句是不是"今天到这儿"的收尾话 —— 是的话她就不回了。

    判得很保守：只认 ≤10 字的短句，而且整句就是收尾话（允许"晚安啦"这种尾巴）。
    长句里捎带一个"晚安"不算，那种她该接。
    """
    if not getattr(getattr(ctx, "cfg", None), "closing_enabled", True):
        return False
    raw = str(text or "")
    if "?" in raw or "？" in raw:      # 问句不是收尾（"睡了？" 是在问，不是道晚安）
        return False
    flat = _flat(raw)
    if not flat or len(flat) > 10:
        return False
    for word in closing_words(ctx):
        head = _flat(word)
        if not head:
            continue
        if flat == head:
            return True
        if (
            flat.startswith(head)
            and len(flat) - len(head) <= 2
            and set(flat[len(head):]) <= set(_CLOSING_TAIL)
        ):
            return True
    return False


def voice_hint(ctx) -> str:
    """有 voice 工具时才教格式：voice_tts 只给路径，发语音靠 <audio src> 这一段。"""
    pool = ctx.services.get("mcppool") if getattr(ctx, "services", None) else None
    servers = getattr(pool, "servers", None) or {}
    if "voice" not in servers:
        return ""
    return (
        "\n\n【发语音】你要发语音时，先调 voice_tts 把话合成出来，"
        '再把返回的文件路径原样放进 <audio src="路径"></audio> 里单独一行，'
        "我会把它当成一条语音发出去（不要把路径当文字说给爸听）。"
        "路径只能用 voice_tts 返回的那一个，别自己编。"
        # 2026-11-08：她会连着合成三条再卡住（工具轮数吃满）。一条回复一条语音就够。
        "一条回复只合成一次，合完就输出那行 <audio src>，别再重新合成第二遍。"
    )


async def run_turn(ctx, batch: list[Message]) -> None:
    conv_id = batch[0].conv_id
    turn_id = batch[-1].message_id
    model = ctx.cfg.model.model
    user_text = render_batch(batch, ctx.cfg)
    remote = is_owner_private(batch[0], ctx.cfg)

    # 每轮都从库里拼「刚才聊了什么」（按人合并几个号）：不靠内存，重启/换会话都不会断
    owner_val = ctx.cfg
    history = load_history(ctx, conv_id, {str(m.message_id) for m in batch}, owner_val)

    system = system_prompt(ctx) + lexicon_block(ctx) + voice_hint(ctx)
    if batch[0].kind == "group":
        system += group_format_hint()
    system += call_hint(ctx, batch[0])
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
        *history,
        {"role": "user", "content": user_text},
    ]

    extra_tools = None
    extra_call = None
    if remote:
        extra_tools = [SEND_TOOL]

        async def extra_call(name, args):
            if name != "send_group_message":
                return None
            return await call_send_tool(ctx, args)

    try:
        reply, usage = await talk(
            ctx, messages, "owner" if remote else "all", extra_tools, extra_call
        )
    except Exception as exc:
        ctx.log.error("模型调用失败 conv=%s：%s", conv_id, exc)
        return

    if not reply:
        ctx.log.warning("模型返回空，不发送 conv=%s（tok=%s）", conv_id, usage)
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
        await send_lines(ctx, conv_id, reply)
    except Exception as exc:
        ctx.log.error("发送失败 conv=%s：%s", conv_id, exc)
        return
    # 她自己说的话也进记忆库（speaker=self）：放在发出去之后，没发成功就不算她说过
    _ = asyncio.create_task(
        remember_own(ctx, conv_id, reply, str(turn_id), _card_who(ctx, batch[0].sender_id))
    )
    try:
        ctx.store.message_add(conv_id, "out", reply, plugin_id=ctx.id)
    except Exception as exc:
        ctx.log.warning("写回复记录失败：%s", exc)
    # 回复已经写进库里了，下一轮 load_history 会自己带上；这里只把内存那份对齐（别的插件可能读它）
    keep = [
        line.strip()
        for line in re.split(r"\n+", _AUDIO_TAG.sub("[语音]", _STICKER_ANY.sub("", reply)))
        if line.strip()
    ]
    if len(keep) > 3:
        keep = keep[:2] + [" ".join(keep[2:])]
    hkey = history_key(ctx.cfg, conv_id)
    remember_turn(hkey, "user", user_text)
    remember_turn(hkey, "assistant", "\n".join(keep))
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