"""表情包：海螺自带的本事，不是插件。

四件事
------
1. **同步 QQ「我的表情」**：调 OneBot 的 `fetch_custom_face`，60 秒缓存，落库存着；
2. **收群友的表情**：有人发表情/图片（带表情标记）就存一份，按 sha1 去重，
   带频率限制（默认每分钟 2 张、每小时 10 张），跟 bridge 原版一致；
3. **进提示词**：`speech.prompt` 里附一句「手里有哪些常用表情」，
   她不用知道全部，知道最常用的几个就够（默认 8 个）；
4. **能发**：以 image 段发出去，发过就把次数 +1。

不做的事：不主动挑表情刷屏。发不发由说话那边决定。

设存放 `config.json` 的 `stickers` 块；面板「表情包」页可以直接改，
改完立刻生效（每次都现读 cfg.raw，不缓存）。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any

from . import log
from .bus import Bus
from .config import Config
from .message import Segment
from .onebot import OneBotClient
from .store import Store

MAX_BYTES = 8 * 1024 * 1024  # 原版 media.maxBytes 也是 8MB
TIMEOUT = 10

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "sync_ttl_ms": 60000,
    "max_list_count": 100,
    "include_in_prompt": True,
    "prompt_max_stickers": 8,
    "collect_enabled": True,
    "collect_max_per_minute": 2,
    "collect_max_per_hour": 10,
    "max_remark_chars": 20,
}

# 面板画表单用：label / 单位 / 范围 / 一句说明
FIELDS: list[dict[str, Any]] = [
    {"key": "enabled", "label": "启用表情包", "type": "bool",
     "hint": "关掉之后不再收、也不再往提示词里塞"},
    {"key": "collect_enabled", "label": "收群友发的表情", "type": "bool",
     "hint": "看到群里的表情/贴图就存一份，翻旧账时能用"},
    {"key": "collect_max_per_minute", "label": "每分钟最多收", "type": "int", "min": 0, "max": 60, "unit": "张"},
    {"key": "collect_max_per_hour", "label": "每小时最多收", "type": "int", "min": 0, "max": 500, "unit": "张"},
    {"key": "include_in_prompt", "label": "告诉她手里有哪些表情", "type": "bool",
     "hint": "在提示词里附一句常用表情名，发的时候她自己的挑"},
    {"key": "prompt_max_stickers", "label": "提示词里列几个", "type": "int", "min": 0, "max": 30, "unit": "个"},
    {"key": "sync_ttl_ms", "label": "QQ 收藏缓存", "type": "int", "min": 0, "max": 3600000, "unit": "毫秒",
     "hint": "多久重新拉一次「我的表情」，0 = 每次都拉"},
    {"key": "max_remark_chars", "label": "备注最长", "type": "int", "min": 2, "max": 40, "unit": "字"},
]


class Stickers:
    def __init__(self, cfg: Config, store: Store, onebot: OneBotClient, bus: Bus | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self.onebot = onebot
        self.bus = bus
        self.log = log.get("stickers")
        self._collected: deque[float] = deque()
        self._sync_at = 0.0
        self._task: asyncio.Task | None = None

    # ---------- 设置 ----------

    @property
    def raw(self) -> dict[str, Any]:
        got = (self.cfg.raw or {}).get("stickers")
        return got if isinstance(got, dict) else {}

    def settings(self) -> dict[str, Any]:
        """当前生效的值：默认值 ← 兼容老写法 ← config.json。"""
        out = dict(DEFAULTS)
        raw = self.raw
        for key in DEFAULTS:
            if key in raw:
                out[key] = raw[key]
        # 兼容 bridge 原版的嵌套写法 collect.{enabled,maxPerMinute,maxPerHour}
        collect = raw.get("collect")
        if isinstance(collect, dict):
            if "enabled" in collect:
                out["collect_enabled"] = bool(collect["enabled"])
            if "maxPerMinute" in collect:
                out["collect_max_per_minute"] = int(collect["maxPerMinute"] or 0)
            if "maxPerHour" in collect:
                out["collect_max_per_hour"] = int(collect["maxPerHour"] or 0)
        return out

    def schema(self) -> list[dict[str, Any]]:
        """给面板画表单：字段定义 + 当前值。"""
        cur = self.settings()
        return [{**f, "value": cur.get(f["key"])} for f in FIELDS]

    def apply(self, values: dict[str, Any]) -> dict[str, Any]:
        """把面板提交的值写进 cfg.raw（落盘由调用方负责），返回新值。"""
        block = self.raw.copy()
        for field in FIELDS:
            key = field["key"]
            if key not in values:
                continue
            val = values[key]
            if field["type"] == "bool":
                block[key] = bool(val)
            elif field["type"] in ("int", "float"):
                try:
                    num = int(val)
                except (TypeError, ValueError):
                    continue
                lo, hi = field.get("min"), field.get("max")
                if lo is not None:
                    num = max(lo, num)
                if hi is not None:
                    num = min(hi, num)
                block[key] = num
            else:
                block[key] = str(val)
        self.cfg.raw["stickers"] = block
        return self.settings()

    # ---------- 1. 同步 QQ 收藏 ----------

    @property
    def dir(self) -> Path:
        path = Path(self.cfg.data_dir) / "stickers"
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def sync(self, force: bool = False) -> int:
        """从协议端拉「我的表情」，写进库。返回新增条数。"""
        cfg = self.settings()
        ttl = int(cfg.get("sync_ttl_ms") or 0) / 1000.0
        if not force and ttl > 0 and (time.time() - self._sync_at) < ttl:
            return 0
        try:
            data = await self.onebot.call("fetch_custom_face")
        except Exception as exc:
            self.log.debug("拉 QQ 收藏表情失败：%s", exc)
            return 0
        rows = []
        for item in data or []:
            if isinstance(item, str):
                rows.append({"url": item})
            elif isinstance(item, dict):
                url = item.get("url") or item.get("file") or item.get("path") or item.get("image") or ""
                if url:
                    rows.append({"url": str(url), "summary": str(item.get("summary") or "")})
        added = self.store.sticker_sync_favorites(rows, ts=int(time.time() * 1000))
        self._sync_at = time.time()
        if added:
            self.log.info("同步 QQ 收藏表情：新增 %d 张（共 %d 张）", added, self.store.sticker_count())
        return added

    # ---------- 2. 收群友的表情 ----------

    @staticmethod
    def _is_sticker(seg: dict) -> bool:
        typ = str(seg.get("type") or "")
        data = seg.get("data") or {}
        if typ in ("mface", "face"):
            return True
        if typ == "image":
            sub = str(data.get("sub_type") or data.get("subType") or "")
            if sub in ("1", "sticker", "face"):
                return True
            summary = str(data.get("summary") or "")
            if summary.startswith("[") and "表情" in summary:
                return True
        return False

    @staticmethod
    def _seg_url(seg: dict) -> str:
        data = seg.get("data") or {}
        for key in ("url", "file", "path"):
            val = str(data.get(key) or "")
            if val.startswith("http"):
                return val
        return ""

    @staticmethod
    def _seg_summary(seg: dict) -> str:
        data = seg.get("data") or {}
        for key in ("summary", "text", "emoji_id"):
            val = str(data.get(key) or "").strip()
            if val:
                return val[:80]
        return str(seg.get("type") or "")

    def _rate_ok(self, cfg: dict) -> bool:
        now = time.time()
        while self._collected and now - self._collected[0] > 3600:
            self._collected.popleft()
        per_min = int(cfg.get("collect_max_per_minute") or 0)
        per_hour = int(cfg.get("collect_max_per_hour") or 0)
        if per_min > 0 and sum(1 for t in self._collected if now - t <= 60) >= per_min:
            return False
        if per_hour > 0 and len(self._collected) >= per_hour:
            return False
        return True

    @staticmethod
    def _download(url: str) -> bytes | None:
        if not url:
            return None
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ConchSticker/2.0"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = resp.read(MAX_BYTES + 1)
            if not data or len(data) > MAX_BYTES:
                return None
            return data
        except Exception:
            return None

    def _save_bytes(self, data: bytes) -> Path:
        digest = hashlib.sha1(data).hexdigest()[:20]
        ext = ".gif" if data[:6] in (b"GIF87a", b"GIF89a") else ".png"
        if data[:2] == b"\xff\xd8":
            ext = ".jpg"
        path = self.dir / f"{digest}{ext}"
        if not path.exists():
            path.write_bytes(data)
        return path

    def collect(self, payload: dict) -> int:
        cfg = self.settings()
        if not cfg.get("collect_enabled", True):
            return 0
        segs = payload.get("segments") or []
        if not segs:
            return 0
        saved = 0
        for seg in segs:
            if not isinstance(seg, dict) or not self._is_sticker(seg):
                continue
            if not self._rate_ok(cfg):
                self.log.debug("收表情到限额了，跳过")
                break
            summary = self._seg_summary(seg)
            url = self._seg_url(seg)
            data = self._download(url) if url else None
            ts = int(time.time() * 1000)
            if data:
                path = self._save_bytes(data)
                self.store.sticker_add(
                    hashlib.sha1(data).hexdigest()[:20],
                    url=url,
                    file=str(path),
                    summary=summary,
                    sender_id=str(payload.get("sender_id") or ""),
                    sender_name=str(payload.get("sender_name") or ""),
                    conv_id=str(payload.get("conv_id") or ""),
                    ts=ts,
                    source="collected",
                )
                self._collected.append(time.time())
                saved += 1
            elif str(seg.get("type")) in ("mface", "face"):
                digest = "txt:" + hashlib.sha1(summary.encode("utf-8")).hexdigest()[:16]
                self.store.sticker_add(
                    digest,
                    url=url,
                    file="",
                    summary=summary,
                    sender_id=str(payload.get("sender_id") or ""),
                    sender_name=str(payload.get("sender_name") or ""),
                    conv_id=str(payload.get("conv_id") or ""),
                    ts=ts,
                    source="collected",
                )
                self._collected.append(time.time())
                saved += 1
        return saved

    def on_observed(self, payload: dict) -> None:
        """总线钩子：收藏失败绝不影响聊天。"""
        try:
            cfg = self.settings()
            if not cfg.get("enabled", True) or not cfg.get("collect_enabled", True):
                return
            n = self.collect(payload)
            if n:
                self.log.debug("收了 %d 张表情包", n)
        except Exception as exc:
            self.log.warning("收表情包出错：%s", exc)

    # ---------- 3. 进提示词 ----------

    @staticmethod
    def _label(row: dict) -> str:
        label = str(row.get("remark") or row.get("summary") or "").strip()
        label = label.replace("[", "").replace("]", "")
        if not label:
            label = "表情"
        return label[:12]

    def on_prompt(self, payload: dict) -> None:
        cfg = self.settings()
        if not cfg.get("enabled", True) or not cfg.get("include_in_prompt", True):
            return
        try:
            rows = self.store.sticker_recent(limit=int(cfg.get("prompt_max_stickers") or 8))
        except Exception:
            return
        usable = [r for r in rows if r.get("file") or r.get("url") or r.get("summary")]
        extra = str(payload.get("extra") or "")
        if not usable:
            tip = (
                "[表情包] 本地库现在是空的。想发表情照样单独占一行写「【表情包：描述】」，"
                "会从「表情包收录库」里挑一张发；别用 QQ 自带表情/emoji 凑数。"
            )
            payload["extra"] = (extra + "\n" + tip) if extra else tip
            return
        names = "、".join(self._label(r) for r in usable)
        try:
            total = self.store.sticker_count()
        except Exception:
            total = 0
        line = (
            "[表情包] 手里有 %d 张表情，常用的这几张：%s。\n"
            "要发就单独占一行写「【表情包：名字】」，名字从上头这些里挑；"
            "对不上名字会去大厅里挑一张。别用 QQ 自带表情/emoji 凑数。" % (total, names)
        )
        payload["extra"] = (extra + "\n" + line) if extra else line

    # ---------- 4. 发出去 ----------

    @staticmethod
    def target_of(row: dict) -> str:
        file = str(row.get("file") or "")
        if file and Path(file).exists():
            return file
        return str(row.get("url") or "")

    async def send(self, conv_id: str, row: dict) -> bool:
        """把一张表情发到会话里。QQ 收藏可能只有 url，那也能发。"""
        target = self.target_of(row)
        if not target:
            return False
        try:
            await self.onebot.send(conv_id, [Segment("image", {"file": target})])
        except Exception as exc:
            self.log.warning("发表情失败：%s", exc)
            return False
        try:
            self.store.sticker_mark_used(int(row["id"]))
        except Exception:
            pass
        return True

    async def pick_and_send(self, conv_id: str, hint: str = "") -> bool:
        """按备注找一张，找不到就随机一张。"""
        row = None
        if hint:
            row = self.store.sticker_find(hint)
        if row is None:
            row = self.store.sticker_random()
        if row is None:
            return False
        return await self.send(conv_id, row)

    # ---------- 生命周期 ----------

    async def _loop(self) -> None:
        while True:
            try:
                if self.settings().get("enabled", True):
                    await self.sync()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.debug("同步循环出错：%s", exc)
            await asyncio.sleep(120)

    def attach(self, bus: Bus) -> None:
        """挂到总线上（宿主启动时调一次）。"""
        self.bus = bus
        bus.on("message.observed", self.on_observed, priority=90, owner="stickers")
        bus.on("speech.prompt", self.on_prompt, priority=60, owner="stickers")
        self.log.info(
            "表情包就绪（已收 %d 张，其中 QQ 收藏 %d 张）",
            self.store.sticker_count(),
            self.store.sticker_count(source="qq_favorite"),
        )

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._loop())

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None