"""合流队列：同一会话连发多条，等窗口结束合成一批。

结果就是文档里定的那条：连发 5 条 = 一次理解、一次回复。

2026-11-07 补两条规则（那天被连着回了两遍）：
1. **总等待不设上限**（本人定）：只在最后一条之后等 `window`，她一直补就一直等；
   `max_wait_ms` 留成 0 就是"不限"，谁要限就自己填。
2. **同一条消息只处理一次**：协议端重连、重复推送都会把同一条再喂进来，
   按 message_id 去重，免得多回一遍。

另外：一批正在思考时，同一会话后来的消息由插件那边加锁排队 + 带上下文，
不在这里处理。
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from .message import Message

BatchHandler = Callable[[list[Message]], Awaitable[None]]


class Merger:
    def __init__(self, window_ms: int, on_batch: BatchHandler,
                 max_wait_ms: int = 0):
        self.window = max(0, window_ms) / 1000.0
        cap = max(0, int(max_wait_ms or 0)) / 1000.0
        # 0 = 不设上限：一直等到对方不再补为止
        self.max_wait = float("inf") if cap <= 0 else max(self.window, cap)
        self._buf: dict[str, list[Message]] = {}
        self._timers: dict[str, asyncio.Task] = {}
        self._first_at: dict[str, float] = {}
        self._seen: set[str] = set()
        self._on_batch = on_batch

    def _dup(self, msg: Message) -> bool:
        mid = str(getattr(msg, "message_id", "") or "")
        if not mid:
            return False
        if mid in self._seen:
            return True
        self._seen.add(mid)
        if len(self._seen) > 4000:          # 别无限长
            self._seen = set(list(self._seen)[-2000:])
        return False

    def feed(self, msg: Message) -> None:
        if self._dup(msg):
            return
        conv = msg.conv_id
        now = time.monotonic()
        if conv not in self._buf:
            self._buf[conv] = []
            self._first_at[conv] = now
        self._buf[conv].append(msg)

        old = self._timers.pop(conv, None)
        if old is not None:
            old.cancel()

        waited = (now - self._first_at.get(conv, now)) * 1000.0
        if self.window <= 0 or waited >= self.max_wait * 1000.0:
            # 等够了，或者本来就不等：立刻合流
            self._timers[conv] = asyncio.create_task(self._fire(conv))
        else:
            self._timers[conv] = asyncio.create_task(self._wait_then_fire(conv))

    async def _wait_then_fire(self, conv: str) -> None:
        try:
            await asyncio.sleep(self.window)
        except asyncio.CancelledError:
            return
        await self._fire(conv)

    async def _fire(self, conv: str) -> None:
        batch = self._buf.pop(conv, [])
        self._timers.pop(conv, None)
        self._first_at.pop(conv, None)
        if batch:
            await self._on_batch(batch)

    async def close(self) -> None:
        for task in list(self._timers.values()):
            task.cancel()
        self._timers.clear()
        self._buf.clear()
        self._first_at.clear()