"""表情包：海螺自带的本事，不是插件。

四件事
------
1. **同步 QQ「我的表情」**：调 OneBot 的 `fetch_custom_face`，60 秒缓存，落库存着；
2. **收群友的表情**：先交视觉模型看懂（一句意思 + 几个标签），再决定收不收 ——
   默认是「她想要才收」：先放候选池，她说要（或主人在面板点「收下」）才算她的；
3. **进提示词**：`speech.prompt` 里附一句「手里有哪些表情」，带意思和标签，
   她挑的时候知道在挑什么；
4. **能发**：以 image 段发出去，发过就把次数 +1。

两道闸防重复花钱
----------------
- `sticker_vision` 表按 sha1 记账：同一张图永远只调一次视觉模型；
- 同时在跑的时候用 inflight 集合挡住并发重复调用，外加每天上限。

不做的事：不主动挑表情刷屏。发不发由说话那边决定。

设存放 `config.json` 的 `stickers` 块；面板「表情包」页可以直接改，
改完立刻生效（每次都现读 cfg.raw，不缓存）。
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import subprocess
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
    "collect_mode": "want",          # want = 她想要才收（先候选）| auto = 看到就收 | off = 不收
    "collect_enabled": True,         # 老写法，只用来推 collect_mode
    "collect_max_per_minute": 5,
    "collect_max_per_hour": 30,
    "candidate_days": 7,
    "candidate_max": 100,             # 缓存区最多存几张（0 = 不限）
    "cache_max_items": 200,           # 缓存区文件上限（张）
    "cache_max_mb": 200,              # 缓存区上限（MB）
    "understand_enabled": True,
    "understand_daily_max": 30,
    "understand_retry_max": 3,        # 一张最多试几次
    "understand_queue_max": 200,      # 缓冲区最多排几张
    "vision_keep_days": 90,           # 没收录的「理解记录」留几天
    "prompt_tags_max": 2,             # 提示词里每张给几个标签
    "candidate_prompt_max": 3,        # 提示词里列几张候选
    "max_remark_chars": 20,
}

# 失败重试的退避：1 分钟 → 5 分钟 → 20 分钟
RETRY_DELAYS = (60, 300, 1200)

COLLECT_MODES = (
    {"value": "want", "label": "想要才收（先放候选）"},
    {"value": "auto", "label": "看到就收"},
    {"value": "off", "label": "不收"},
)

# 面板画表单用：group 分组，advanced 折进「高级」；label / 单位 / 范围 / 一句说明
FIELDS: list[dict[str, Any]] = [
    # ── 收不收 ──
    {"key": "enabled", "group": "基本", "label": "启用", "type": "bool",
     "hint": "关掉就不再收、也不再往提示词里塞"},
    {"key": "collect_mode", "group": "基本", "label": "怎么收", "type": "select", "options": COLLECT_MODES,
     "hint": "默认想要才收；「自动」看到就收，「不收」彻底关"},
    {"key": "candidate_days", "group": "基本", "label": "缓存区留几天", "type": "int", "min": 0, "max": 60, "unit": "天",
     "hint": "没被收下的图在这儿存几天，过期自动清掉（图一起删）；0 = 一直留"},
    {"key": "collect_max_per_minute", "group": "基本", "label": "每分钟最多收", "type": "int", "min": 0, "max": 60, "unit": "张",
     "hint": "防刷屏的硬闸；0 = 不限"},
    {"key": "collect_max_per_hour", "group": "基本", "label": "每小时最多收", "type": "int", "min": 0, "max": 500, "unit": "张",
     "hint": "同上，按小时算"},
    # ── 看图 ──
    {"key": "understand_enabled", "group": "看图", "label": "用视觉模型看懂", "type": "bool",
     "hint": "收之前先看一眼：一句意思 + 几个标签。同一张只调一次"},
    {"key": "understand_daily_max", "group": "看图", "label": "每天最多看", "type": "int", "min": 0, "max": 500, "unit": "张",
     "hint": "只算看成功的；0 = 不限"},
    {"key": "understand_retry_max", "group": "看图", "label": "一张最多试", "type": "int", "min": 1, "max": 10, "unit": "次",
     "hint": "失败先放缓冲区，按 1 / 5 / 20 分钟退避重试"},
    {"key": "understand_queue_max", "group": "看图", "label": "缓冲区最多排", "type": "int", "min": 0, "max": 2000, "unit": "张",
     "hint": "太多了丢最旧的；0 = 不限"},
    {"key": "vision_keep_days", "group": "看图", "label": "理解记录留", "type": "int", "min": 0, "max": 3650, "unit": "天",
     "hint": "没收录的图「看过没」记这么久；已收下的永久留；0 = 一直留"},
    # ── 容量 ──
    {"key": "candidate_max", "group": "容量", "label": "缓存区最多留", "type": "int", "min": 0, "max": 2000, "unit": "张",
     "hint": "还没收的图，满了先淘汰最久没出现的"},
    {"key": "cache_max_items", "group": "容量", "label": "缓存文件最多", "type": "int", "min": 0, "max": 5000, "unit": "个",
     "hint": "按文件数算的硬上限"},
    {"key": "cache_max_mb", "group": "容量", "label": "缓存最大占", "type": "int", "min": 0, "max": 8192, "unit": "MB",
     "hint": "按体积算的硬上限；和上一项谁先到谁淘汰"},
    # ── 提示词 ──
    {"key": "include_in_prompt", "group": "提示词", "label": "提示词里带上表情", "type": "bool",
     "hint": "提示词里附一句常用表情（带意思），发的时候自己挑"},
    {"key": "prompt_max_stickers", "group": "提示词", "label": "列几张", "type": "int", "min": 0, "max": 30, "unit": "张"},
    {"key": "prompt_tags_max", "group": "提示词", "label": "每张带几个标签", "type": "int", "min": 0, "max": 5, "unit": "个",
     "hint": "省 token 就调小"},
    {"key": "candidate_prompt_max", "group": "提示词", "label": "列几张候选", "type": "int", "min": 0, "max": 10, "unit": "张",
     "hint": "让模型判断要不要收；0 = 不列"},
    # ── 高级（默认收起）──
    {"key": "sync_ttl_ms", "group": "高级", "advanced": True,
     "label": "QQ 收藏缓存", "type": "int", "min": 0, "max": 3600000, "unit": "毫秒",
     "hint": "多久重新拉一次「我的表情」；0 = 每次都拉"},
    {"key": "max_remark_chars", "group": "高级", "advanced": True,
     "label": "备注最长", "type": "int", "min": 2, "max": 40, "unit": "字"},
]


KEEP_TOOL = {
    "type": "function",
    "function": {
        "name": "keep_sticker",
        "description": (
            "把候选池里的表情收下，留着以后自己发。群里会不断冒出新的表情（候选），"
            "看到觉得好玩、以后可能想发的就收一张；收不收随你，不想收就不用调。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "要收哪一张：说名字或意思里的关键词（比如「猫」「无语」）；不写就收最新那张",
                }
            },
            "required": [],
        },
    },
}


class Stickers:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        onebot: OneBotClient,
        bus: Bus | None = None,
        tools: Any = None,
        look: Any = None,
        services: dict[str, Any] | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.onebot = onebot
        self.bus = bus
        self.tools = tools
        self.look = look
        self.services = services or {}
        self.log = log.get("stickers")
        self._collected: deque[float] = deque()
        self._sync_at = 0.0
        self._task: asyncio.Task | None = None
        # 视觉模型那一路：一次只看一张，同一张不重复看
        self._vision_lock = asyncio.Semaphore(1)
        self._vision_inflight: set[str] = set()

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
        # 老配置里只有 collect_enabled（bool）：换算成新的三档
        if "collect_mode" not in raw:
            if "collect_enabled" in raw:
                out["collect_mode"] = "auto" if out.get("collect_enabled", True) else "off"
            else:
                out["collect_mode"] = DEFAULTS["collect_mode"]
        if str(out.get("collect_mode")) not in ("want", "auto", "off"):
            out["collect_mode"] = "want"
        return out

    def mode(self) -> str:
        return str(self.settings().get("collect_mode") or "want")

    def schema(self) -> list[dict[str, Any]]:
        """给面板画表单：字段定义 + 当前值 + 默认值（「恢复默认」用）。"""
        cur = self.settings()
        return [
            {**f, "value": cur.get(f["key"]), "default": DEFAULTS.get(f["key"])}
            for f in FIELDS
        ]

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

    @property
    def cache_dir(self) -> Path:
        """缓存区：还没被她收下的图先放这儿。

        和正式目录分开，才能单独算占用、单独淘汰，也不至于清缓存把她的收藏一起删了。
        """
        path = self.dir / "_cache"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def cache_bytes(self) -> int:
        total = 0
        try:
            for f in self.cache_dir.rglob("*"):
                if f.is_file() and "_frames" not in f.parts:
                    total += f.stat().st_size
        except OSError:
            pass
        return total

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

    def _save_bytes(self, data: bytes, cache: bool = False) -> Path:
        digest = hashlib.sha1(data).hexdigest()[:20]
        ext = ".gif" if data[:6] in (b"GIF87a", b"GIF89a") else ".png"
        if data[:2] == b"\xff\xd8":
            ext = ".jpg"
        base = self.cache_dir if cache else self.dir
        path = base / f"{digest}{ext}"
        # 缓存区命中的可能已经在正式目录里了（她之前收过又删了）：别再存一份
        other = (self.dir if cache else self.cache_dir) / f"{digest}{ext}"
        if other.exists():
            return other
        if not path.exists():
            path.write_bytes(data)
        return path

    # 库里只存文件名（stickers/xxx.gif），不存绝对路径：
    # 整个安装目录搬走之后，旧路径就全指不到了（面板显示一堆裂图）。
    def _name_of(self, path: Path) -> str:
        try:
            # 正斜杠：库里的路径别跟着系统变（Windows 存成 `_cache\x.gif` 面板/比对都不好看）
            return path.relative_to(self.dir).as_posix()
        except ValueError:
            return path.name

    def resolve(self, file: str) -> Path | None:
        """把库里的 file 字段还原成真实路径（相对名 / 老绝对路径都认）。"""
        name = str(file or "").strip()
        if not name:
            return None
        path = Path(name)
        if not path.is_absolute():
            path = self.dir / name
        return path if path.exists() else None

    def collect(self, payload: dict) -> list[tuple[int, str, Path | None]]:
        """看到表情就处理一张。返回 [(id, hash, 本地文件)]，后面再交视觉模型看懂。

        - 已经见过的（库里有 / 视觉账上有）直接跳过，不重复下载、不重复调模型；
        - 收藏方式 want → 先进候选池；auto → 直接算她的。
        """
        cfg = self.settings()
        mode = self.mode()
        if mode == "off":
            return []
        segs = payload.get("segments") or []
        if not segs:
            return []
        source = "candidate" if mode == "want" else "collected"
        out: list[tuple[int, str, Path | None]] = []
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
                digest = hashlib.sha1(data).hexdigest()[:20]
                if self.store.sticker_by_hash(digest) is not None:
                    # 见过（可能还在缓存区 / 已经很她收下）：记一次「最近出现」，别重复下载理解
                    self.store.sticker_touch(digest)
                    self._collected.append(time.time())
                    continue
                path = self._save_bytes(data, cache=(source == "candidate"))
                sid = self.store.sticker_add(
                    digest,
                    url=url,
                    file=self._name_of(path),
                    summary=summary,
                    sender_id=str(payload.get("sender_id") or ""),
                    sender_name=str(payload.get("sender_name") or ""),
                    conv_id=str(payload.get("conv_id") or ""),
                    ts=ts,
                    source=source,
                )
                self._collected.append(time.time())
                out.append((sid, digest, path))
            elif str(seg.get("type")) in ("mface", "face"):
                digest = "txt:" + hashlib.sha1(summary.encode("utf-8")).hexdigest()[:16]
                if self.store.sticker_by_hash(digest) is not None:
                    continue
                sid = self.store.sticker_add(
                    digest,
                    url=url,
                    file="",
                    summary=summary,
                    sender_id=str(payload.get("sender_id") or ""),
                    sender_name=str(payload.get("sender_name") or ""),
                    conv_id=str(payload.get("conv_id") or ""),
                    ts=ts,
                    source=source,
                )
                self._collected.append(time.time())
                out.append((sid, digest, None))
        return out

    def on_observed(self, payload: dict) -> None:
        """总线钩子：收藏失败绝不影响聊天。"""
        try:
            cfg = self.settings()
            if not cfg.get("enabled", True) or self.mode() == "off":
                return
            rows = self.collect(payload)
            if not rows:
                return
            self.log.debug("收了 %d 张表情包（%s）", len(rows), self.mode())
            if cfg.get("understand_enabled", True) and self.look is not None:
                asyncio.ensure_future(self._enrich(rows))
        except Exception as exc:
            self.log.warning("收表情出错：%s", exc)

    # ---------- 2.5 让视觉模型看懂 ----------

    async def describe(self, digest: str, path: Path) -> tuple[str, str]:
        """看一张表情，返回 (一句意思, 标签)。

        三道门（都在调模型之前）：
        - 看成了 → 直接用账上的，永远不再调；
        - 在缓冲区且还没到重试时间 / 已经试满次数 → 跳过；
        - 今天额度用完 → 跳过。
        没看成 → 进缓冲区，记第几次、下次什么时候试（1 / 5 / 20 分钟退避）。
        """
        cfg = self.settings()
        if not cfg.get("understand_enabled", True) or self.look is None:
            return "", ""
        now = int(time.time() * 1000)
        cached = self.store.sticker_vision_get(digest)
        if cached and cached.get("status") == "ok":
            return str(cached.get("meaning") or ""), str(cached.get("tags") or "")
        if digest in self._vision_inflight:
            return "", ""
        retry_max = int(cfg.get("understand_retry_max") or 3)
        if cached and cached.get("status") == "fail":
            if int(cached.get("attempts") or 0) >= retry_max:
                return "", ""  # 试满了：标「没看懂」，留给面板手填
            if int(cached.get("next_at") or 0) > now:
                return "", ""  # 缓冲区还没到点
        cap = int(cfg.get("understand_daily_max") or 0)
        if cap > 0 and self.store.sticker_vision_count_today() >= cap:
            self.log.info("今天看表情到上限（%d 张），剩下的明天再看", cap)
            return "", ""
        self._vision_inflight.add(digest)
        try:
            async with self._vision_lock:
                cached = self.store.sticker_vision_get(digest)
                if cached and cached.get("status") == "ok":
                    return str(cached.get("meaning") or ""), str(cached.get("tags") or "")
                target = self._still_image(path)
                seen = await self.look.see_sticker(str(target))
                if not seen:
                    self._note_failure(digest, retry_max)
                    return "", ""
                meaning = str(seen.get("desc") or "").strip()
                tags = "、".join(
                    str(t).strip() for t in (seen.get("tags") or []) if str(t).strip()
                )
                self.store.sticker_vision_put(
                    digest, meaning, tags, str(seen.get("model") or "")
                )
                return meaning, tags
        finally:
            self._vision_inflight.discard(digest)

    def _note_failure(self, digest: str, retry_max: int) -> None:
        """没看成：排进缓冲区，按 1 / 5 / 20 分钟退避。"""
        cached = self.store.sticker_vision_get(digest)
        tried = int(cached.get("attempts") or 0) if cached else 0
        if tried >= retry_max:
            return
        delay = RETRY_DELAYS[min(tried, len(RETRY_DELAYS) - 1)]
        attempts = self.store.sticker_vision_fail(
            digest, next_at=int(time.time() * 1000) + delay * 1000
        )
        self.log.debug("这张没看懂（第 %d 次），%d 秒后再试", attempts, delay)
        # 缓冲区也得有上限：超了就丢掉最旧的失败记录（图还在候选里，下次出现还能再试）
        queue_max = int(self.settings().get("understand_queue_max") or 0)
        if queue_max > 0:
            self.store.sticker_vision_trim_fails(queue_max)

    def _still_image(self, path: Path) -> Path:
        """动图先抽第一帧：视觉模型啃不动大 GIF，会回「图片格式解析错误」。"""
        if path.suffix.lower() != ".gif":
            return path
        frames = self.dir / "_frames"
        out = frames / f"{path.stem}.png"
        if out.exists():
            return out
        ffmpeg = ""
        try:
            eye_root = str((self.look.settings() or {}).get("eye_root") or "")
        except Exception:
            eye_root = ""
        if eye_root:
            cand = Path(eye_root) / "bin" / "ffmpeg.exe"
            if cand.exists():
                ffmpeg = str(cand)
        if not ffmpeg:
            ffmpeg = shutil.which("ffmpeg") or ""
        if not ffmpeg:
            return path
        try:
            frames.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-i", str(path),
                 "-frames:v", "1", str(out)],
                timeout=30,
                capture_output=True,
                creationflags=0x08000000,
            )
        except Exception as exc:
            self.log.debug("抽帧失败（就把原图给模型）：%s", exc)
            return path
        return out if out.exists() else path

    async def _enrich(self, rows: list[tuple[int, str, Path | None]]) -> None:
        for sid, digest, path in rows:
            try:
                if path is None:
                    continue
                meaning, tags = await self.describe(digest, path)
                if meaning or tags:
                    self.store.sticker_set_meaning(sid, meaning, tags)
            except Exception as exc:
                self.log.debug("看懂表情失败（不影响收）：%s", exc)

    def vision_status(self) -> dict[str, Any]:
        """视觉模型这个「口」现在什么情况：今天用了几次、还剩几次、缓冲区几张。"""
        cfg = self.settings()
        used = self.store.sticker_vision_count_today()
        cap = int(cfg.get("understand_daily_max") or 0)
        buffered = len(self.store.sticker_vision_failed_hashes())
        model = ""
        if self.look is not None:
            try:
                model = str((self.look.settings() or {}).get("model") or "")
            except Exception:
                model = ""
        if not model:
            model = self.store.sticker_vision_last_model()
        return {
            "enabled": bool(cfg.get("understand_enabled", True)),
            "model": model,
            "used_today": used,
            "daily_max": cap,
            "remaining": (max(0, cap - used) if cap > 0 else -1),
            "buffer": buffered,
            "retry_max": int(cfg.get("understand_retry_max") or 3),
            "look_used": self._look_used(),
            "look_max": self._look_max(),
        }

    def _look_used(self) -> int:
        if self.look is None:
            return 0
        try:
            return int(self.look.usage_today())
        except Exception:
            return 0

    def _look_max(self) -> int:
        if self.look is None:
            return 0
        try:
            return int((self.look.settings() or {}).get("max_images_per_day") or 0)
        except Exception:
            return 0

    async def _retry_pending(self) -> None:
        """缓冲区：到点的重新认一遍（模型恢复后自己接着跑）。"""
        cfg = self.settings()
        if not cfg.get("understand_enabled", True) or self.look is None:
            return
        try:
            due = self.store.sticker_vision_due(limit=5)
        except Exception as exc:
            self.log.debug("取缓冲区失败：%s", exc)
            return
        for rec in due:
            digest = str(rec.get("hash") or "")
            row = self.store.sticker_by_hash(digest)
            if row is None:
                continue  # 图已经不在库里了，账留着没意义
            path = self.resolve(str(row.get("file") or ""))
            if path is None:
                continue
            meaning, tags = await self.describe(digest, path)
            if meaning or tags:
                self.store.sticker_set_meaning(int(row["id"]), meaning, tags)
                self.log.info("缓冲区里补看明白一张：%s", meaning[:30])

    def promote(self, sticker_id: int, remark: str = "") -> bool:
        """候选 → 正式收下：文件从缓存区搬进正式目录。

        只动表情包库，不写记忆库 —— 意思和标签在 stickers 表里，记忆库只装她自己的经历。
        """
        row = self.store.sticker_get(int(sticker_id))
        ok = self.store.sticker_promote(int(sticker_id), remark)
        if not ok:
            return False
        if row:
            self._move_out_of_cache(int(sticker_id), str(row.get("file") or ""))
        self.log.info("收下了表情 #%s%s", sticker_id, f"（{remark}）" if remark else "")
        return True

    def _move_out_of_cache(self, sticker_id: int, file: str) -> None:
        """把缓存区里的文件挪进正式目录（不复制），库里的路径跟着改。"""
        if not file:
            return
        src = self.resolve(file)
        if src is None or self.cache_dir not in src.parents:
            return
        try:
            dest = self.dir / src.name
            if dest.exists():
                src.unlink(missing_ok=True)
            else:
                src.replace(dest)
            self.store.sticker_set_file(sticker_id, dest.name)
        except OSError as exc:
            self.log.debug("把表情挪出缓存区失败（不影响发）：%s", exc)

    def keep(self, hint: str = "") -> str:
        """她说要哪张就收哪张；没给名字就收最新的候选。"""
        cands = self.store.sticker_candidates(limit=20)
        if not cands:
            return "候选里没有表情可以收"
        row = None
        text = str(hint or "").strip()
        if text:
            # 先认标签正好等于它（或备注里写着）的，再看包含关系
            for it in cands:
                tags = str(it.get("tags") or "")
                if text == str(it.get("remark") or "") or text in tags.split("、"):
                    row = it
                    break
            else:
                for it in cands:
                    blob = f"{it.get('remark')} {it.get('meaning')} {it.get('tags')} {it.get('summary')}"
                    if text in blob:
                        row = it
                        break
        if row is None:
            label = str(hint or "").strip()
            if label:
                # 她想收「那只猫」但缓存里没有：说没找到，别拿最新那张糊弄她
                return f"缓存区里没找到「{label[:20]}」，没收"
            row = cands[0]
        self.promote(int(row["id"]), text[:40])
        label = str(row.get("remark") or row.get("meaning") or row.get("summary") or "表情")
        return f"收下了：{label[:40]}"

    # ---------- 3. 进提示词 ----------

    @staticmethod
    def _label(row: dict) -> str:
        label = str(row.get("remark") or row.get("meaning") or row.get("summary") or "").strip()
        label = label.replace("[", "").replace("]", "")
        if not label:
            label = "表情"
        return label[:12]

    async def retag(self, sticker_id: int) -> str:
        """按主人写的描述重新算标签（描述不改，只补标签）。"""
        row = self.store.sticker_get(int(sticker_id))
        if row is None or self.look is None:
            return ""
        meaning = str(row.get("meaning") or "").strip()
        path = self.resolve(str(row.get("file") or ""))
        if not meaning or path is None:
            return ""
        try:
            seen = await self.look.see_sticker(str(self._still_image(path)), guidance=meaning)
        except Exception as exc:
            self.log.debug("按描述重算标签失败：%s", exc)
            return ""
        if not seen:
            return ""
        tags = "、".join(str(t).strip() for t in (seen.get("tags") or []) if str(t).strip())
        if tags:
            self.store.sticker_set_meaning(int(sticker_id), meaning, tags)
        return tags

    def _describe_line(self, row: dict) -> str:
        """给提示词用：名字（意思 / 标签），名字本身就是意思时不重复。"""
        name = self._label(row)
        meaning = str(row.get("meaning") or "").strip()
        tags = str(row.get("tags") or "").strip()
        if not meaning and not tags:
            return name
        limit = int(self.settings().get("prompt_tags_max") or 0)
        if limit > 0 and tags:
            tags = "、".join(tags.split("、")[:limit])
        elif limit == 0:
            tags = ""
        if name == (meaning or name)[:12]:
            return f"{name}（{tags}）" if tags else name
        bits = "；".join(b for b in (meaning, tags) if b)
        return f"{name}（{bits[:60]}）"

    def on_prompt(self, payload: dict) -> None:
        cfg = self.settings()
        if not cfg.get("enabled", True) or not cfg.get("include_in_prompt", True):
            return
        try:
            rows = self.store.sticker_recent(limit=int(cfg.get("prompt_max_stickers") or 8))
            cand_limit = int(cfg.get("candidate_prompt_max") or 3)
            candidates = (
                self.store.sticker_candidates(limit=max(cand_limit * 3, 3))
                if self.mode() == "want" and cand_limit > 0 else []
            )
            # 只列「看得懂」的候选：都没看懂时列表全是「动画表情」，她根本没法挑
            candidates = [r for r in candidates if str(r.get("meaning") or r.get("tags") or "").strip()]
            candidates = candidates[:cand_limit]
        except Exception:
            return
        usable = [r for r in rows if r.get("file") or r.get("url") or r.get("summary")]
        extra = str(payload.get("extra") or "")
        parts: list[str] = []
        if usable:
            try:
                total = self.store.sticker_count()
            except Exception:
                total = 0
            names = "、".join(self._describe_line(r) for r in usable)
            parts.append(
                "[表情包] 手里有 %d 张表情，常用的这几张：%s。\n"
                "要发就单独占一行写「【表情包：名字或意思】」，按意思对得上就行；"
                "对不上会去大厅里挑一张。别用 QQ 自带表情/emoji 凑数。" % (total, names)
            )
        else:
            parts.append(
                "[表情包] 本地库现在是空的。想发表情照样单独占一行写「【表情包：描述】」，"
                "会从「表情包收录库」里挑一张发；别用 QQ 自带表情/emoji 凑数。"
            )
        if candidates:
            picks = "、".join(self._describe_line(r) for r in candidates)
            parts.append(
                "[表情包·候选] 群里新看到这几张还没收：%s。\n"
                "觉得有意思就用 keep_sticker 收下（说个名字或意思就行），不收也没关系，"
                "过几天自己会清掉。" % picks
            )
        line = "\n".join(parts)
        payload["extra"] = (extra + "\n" + line) if extra else line

    # ---------- 4. 发出去 ----------

    def target_of(self, row: dict) -> str:
        """要发给协议端的东西：本地文件优先，没有就退回原始 url。"""
        local = self.resolve(row.get("file") or "")
        if local is not None:
            return str(local)
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
                cfg = self.settings()
                if cfg.get("enabled", True):
                    await self.sync()
                    self._expire()
                    await self._retry_pending()
                    self._maintain()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.debug("同步循环出错：%s", exc)
            await asyncio.sleep(60)

    def _maintain(self) -> None:
        """缓存区收拾：张数上限、体积上限、理解记录过期。"""
        cfg = self.settings()
        try:
            max_items = int(cfg.get("candidate_max") or 0)
            files = self.store.sticker_trim_candidates(max_items) if max_items > 0 else []
            self._unlink_files(files)
            # 体积上限：从最旧的一直删到不超标（张数上限已经删过一轮，这里只会更少）
            max_mb = int(cfg.get("cache_max_mb") or 0)
            hard_items = int(cfg.get("cache_max_items") or 0)
            if hard_items > 0:
                extra = self.store.sticker_trim_candidates(hard_items)
                self._unlink_files(extra)
            if max_mb > 0:
                limit = max_mb * 1024 * 1024
                while self.cache_bytes() > limit:
                    oldest = self.store.sticker_oldest_candidates(1)
                    if not oldest:
                        break
                    gone = self.store.sticker_trim_candidates(
                        max(0, self.store.sticker_count(source="candidate") - 1)
                    )
                    self._unlink_files(gone)
                    if not gone:
                        break
            keep_days = int(cfg.get("vision_keep_days") or 0)
            keep_hashes = {str(r.get("hash")) for r in self.store.sticker_list(limit=2000)
                           if r.get("source") != "candidate"}
            removed = self.store.sticker_vision_prune(keep_days, 0, keep_hashes)
            orphans = self.store.sticker_vision_prune_orphans()
            if removed or orphans:
                self.log.debug("理解记录清了 %d 条（其中无主的 %d 条）", removed + orphans, orphans)
        except Exception as exc:
            self.log.debug("收拾缓存区失败：%s", exc)

    def _unlink_files(self, names: list[str]) -> None:
        for name in names:
            path = self.resolve(name)
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _expire(self) -> None:
        """她一直没要的候选，过几天清掉（文件一起删）。"""
        days = int(self.settings().get("candidate_days") or 0)
        if days <= 0:
            return
        try:
            files = self.store.sticker_expire_candidates(days)
        except Exception as exc:
            self.log.debug("清候选失败：%s", exc)
            return
        for name in files:
            path = self.resolve(name)
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        if files:
            self.log.info("清掉了 %d 张她没要的候选表情", len(files))

    def fix_paths(self) -> int:
        """把库里存的老绝对路径收敛成文件名（安装目录搬过家之后老路径全失效）。"""
        fixed = 0
        for sid, file in self.store.sticker_files():
            path = Path(file)
            if not path.is_absolute():
                continue
            # 文件不在了也改成文件名 —— 旧绝对路径怎么都指不到了
            self.store.sticker_set_file(sid, path.name)
            fixed += 1
        if fixed:
            self.log.info("表情包路径已归位：%d 张（改成只记文件名，跟着 data/stickers 走）", fixed)
        return fixed

    def attach(self, bus: Bus) -> None:
        """挂到总线上（宿主启动时调一次）。"""
        self.bus = bus
        try:
            self.fix_paths()
        except Exception as exc:
            self.log.warning("修表情包路径失败（不影响使用）：%s", exc)
        bus.on("message.observed", self.on_observed, priority=90, owner="stickers")
        bus.on("speech.prompt", self.on_prompt, priority=60, owner="stickers")
        if self.tools is not None:
            try:
                self.tools.register(KEEP_TOOL, lambda args: self.keep(str(args.get("name") or "")),
                                    owner="stickers")
            except Exception as exc:
                self.log.warning("挂 keep_sticker 工具失败：%s", exc)
        self.log.info(
            "表情包就绪（已收 %d 张，其中 QQ 收藏 %d 张；收法：%s；看懂：%s）",
            self.store.sticker_count(),
            self.store.sticker_count(source="qq_favorite"),
            self.mode(),
            "开" if self.settings().get("understand_enabled", True) else "关",
        )

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._loop())

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None