"""联网插件：给她一个「自己上网查」的工具，并把查过的短词记进词库。

工具名叫 web_lookup，模型在聊天里遇到不懂的词/梗/事件就会调它。
查完顺手学习：短词 + 词库里没有 + 模型能总结 → 存成一条黑话（来源「联网学习」）。
开关都在 config.json 的 tools 里：web_lookup / web_learn，默认都开。
"""
from __future__ import annotations

import asyncio
import datetime as dt

from qqbridge.core import websearch

OWNER = "websearch"
LEARN_CAP = 20  # 每天最多自动学多少个词

TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "web_lookup",
        "description": (
            "上网查一个你不懂的词、梗、事件、作品或人名。返回几条搜索结果摘要，"
            "够你判断群里在说什么。用一次就够，别连着查好几遍。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要查的东西，可以只写词，也可以写成一句问话",
                }
            },
            "required": ["query"],
        },
    },
}

LEARN_SYS = (
    "你在帮一个 QQ 机器人记群里的黑话。只输出一句中文解释，"
    "不要引号、不要客套、不要重复词条本身，不要写来源。60 字以内。"
)

_NOT_TERM = (
    "什么", "怎么", "意思", "为何", "为什么", "啥", "吗", "呢", "求", "梗",
    "是谁", "出处", "来源", "？", "?", "。", "，", " ", "\n", "\t",
)


def _on(ctx, key: str) -> bool:
    return bool(((ctx.cfg.raw or {}).get("tools") or {}).get(key, True))


def looks_like_term(query: str) -> bool:
    """短、没空格、不像一句问话 —— 这种才值得进词库。"""
    term = (query or "").strip()
    if not (2 <= len(term) <= 6):
        return False
    return not any(bad in term for bad in _NOT_TERM)


def _budget_left(ctx) -> int:
    today = dt.date.today().isoformat()
    day = ctx.store.kv_get(OWNER, "learn_day", "")
    used = int(ctx.store.kv_get(OWNER, "learn_used", 0) or 0)
    return LEARN_CAP if day != today else max(0, LEARN_CAP - used)


def _spend(ctx) -> None:
    today = dt.date.today().isoformat()
    day = ctx.store.kv_get(OWNER, "learn_day", "")
    used = int(ctx.store.kv_get(OWNER, "learn_used", 0) or 0)
    ctx.store.kv_put(OWNER, "learn_day", today)
    ctx.store.kv_put(OWNER, "learn_used", used + 1 if day == today else 1)


async def learn_term(ctx, term: str, items: list[dict]) -> None:
    """把这次查到的资料交给模型压成一句含义，存进词库。失败就静默跳过。"""
    backend = ctx.services.get("backend")
    if backend is None:
        return
    try:
        known = {i["term"] for i in ctx.store.lexicon_list()}
        if term in known:
            return
        if _budget_left(ctx) <= 0:
            ctx.log.info("自动学习今天到上限了（%s）", LEARN_CAP)
            return
        material = websearch.as_material(items, 900)
        prompt = f"群友说的词：「{term}」。"
        if material:
            prompt += f"\n\n网上的说法：\n{material}"
        prompt += "\n\n请写出这个词在群聊语境里的含义。"
        reply, _usage = await backend.chat(
            [{"role": "system", "content": LEARN_SYS}, {"role": "user", "content": prompt}],
            temperature=0.3,
        )
        note = str(reply or "").strip().strip("「」“”\"'").split("\n")[0][:200]
        if len(note) < 6:
            return
        ctx.store.lexicon_add(term, "slang", note, "联网学习")
        _spend(ctx)
        ctx.log.info("自动学会「%s」：%s", term, note[:40])
    except Exception as exc:
        ctx.log.info("自动学习跳过「%s」：%s", term, str(exc)[:80])


async def _handler(ctx, args: dict) -> str:
    query = str(args.get("query") or args.get("q") or args.get("词") or "").strip()[:80]
    if not query:
        return "没给要查的东西"
    items = await websearch.search(query, limit=6)
    if not items:
        items = await websearch.search(f"{query} 是什么意思", limit=6)
    if not items:
        return "网上没查到（可能没网），别编，就说不知道"
    ctx.log.info("联网查「%s」→ %s 条", query, len(items))
    if _on(ctx, "web_learn") and looks_like_term(query):
        asyncio.create_task(learn_term(ctx, query.strip(), items))
    return websearch.as_material(items, 1200)


def setup(ctx) -> None:
    toolbox = ctx.services.get("tools")
    if toolbox is None:
        ctx.log.warning("宿主没有工具盒，联网工具没挂上")
        return
    if not _on(ctx, "web_lookup"):
        ctx.log.info("工具已关（tools.web_lookup=false）")
        return
    toolbox.register(TOOL_SPEC, lambda args: _handler(ctx, args), owner=OWNER)
    ctx.log.info("联网工具就绪（自动学习%s）", "开" if _on(ctx, "web_learn") else "关")


def teardown(ctx) -> None:
    toolbox = ctx.services.get("tools")
    if toolbox is not None:
        removed = toolbox.unregister_owner(OWNER)
        if removed:
            ctx.log.info("联网工具已摘掉（%s 个）", removed)