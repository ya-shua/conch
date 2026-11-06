"""联网查词：360 搜索优先、Bing 兜底。抓结果当资料，喂给模型总结成一句含义。

不带 Key、不依赖别的插件；网络不通就老实返回空列表，别把解释功能一起拖垮。
"""
from __future__ import annotations

import html as _html
import re
import time
from typing import Any

import aiohttp

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
ENDPOINTS = (
    ("360", "https://www.so.com/s", {"q": "{q}"}),
    ("bing", "https://cn.bing.com/search", {"q": "{q}", "setlang": "zh-CN"}),
)
TTL = 900.0
_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

_JUNK = ("其他人还搜了", "相关搜索", "大家还在搜", "猜你想搜")


def _clean(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", "", fragment or "")
    text = _html.unescape(text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    return text.strip()


def _snip(block: str) -> str:
    for pat in (
        r'class="res-list-summary"[^>]*>(.*?)</span>',
        r'class="[^"]*res-desc[^"]*"[^>]*>(.*?)</(?:p|div|span)>',
        r"<p[^>]*>(.*?)</p>",
        r'class="b_caption"[^>]*>(.*?)</div>',
    ):
        m = re.search(pat, block, re.S)
        if m:
            text = _clean(m.group(1))
            if len(text) > 12:
                return text[:300]
    return ""


def _url(block: str) -> str:
    m = re.search(r'href="(https?://[^"]+)"', block)
    if not m:
        return ""
    link = m.group(1)
    for key in ("&url=", "?url="):
        if key in link:
            from urllib.parse import unquote

            link = unquote(link.split(key, 1)[1].split("&")[0])
    return link[:300]


def _parse_360(body: str, limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in re.split(r'<li class="res-list', body or "")[1:]:
        m = re.search(r"<h3[^>]*>(.*?)</h3>", block, re.S)
        title = _clean(m.group(1)) if m else ""
        if not title or any(j in title for j in _JUNK):
            continue
        out.append({"title": title[:120], "url": _url(block), "snippet": _snip(block)})
        if len(out) >= limit:
            break
    return out


def _parse_bing(body: str, limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in re.split(r'<li class="b_algo"', body or "")[1:]:
        m = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        out.append(
            {"title": _clean(m.group(2))[:120], "url": m.group(1)[:300], "snippet": _snip(block)}
        )
        if len(out) >= limit:
            break
    return out


_PARSERS = {"360": _parse_360, "bing": _parse_bing}


async def search(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """搜一次（15 分钟缓存）。先 360，空就换 Bing；都挂了返回空列表，不抛。"""
    q = (query or "").strip()
    if not q:
        return []
    now = time.time()
    hit = _cache.get(q)
    if hit and now - hit[0] < TTL:
        return hit[1]

    for name, url, params in ENDPOINTS:
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(
                    url,
                    params={k: v.format(q=q) for k, v in params.items()},
                    headers=HEADERS,
                    timeout=aiohttp.ClientTimeout(total=12),
                ) as resp:
                    if resp.status != 200:
                        continue
                    body = await resp.text()
        except Exception:
            continue
        items = _PARSERS[name](body, limit)
        items = [i for i in items if i["title"]]
        if items:
            _cache[q] = (now, items)
            return items
    return []


def as_material(items: list[dict[str, Any]], max_chars: int = 1000) -> str:
    """把搜索结果压成几行给模型看。"""
    lines: list[str] = []
    used = 0
    for i, it in enumerate(items, 1):
        title = it.get("title", "")
        snippet = it.get("snippet", "")
        line = f"{i}. {title}：{snippet}".strip("： ") if snippet else f"{i}. {title}"
        if len(line) < 4:
            continue
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)