"""合流队列：同一会话连发多条，等窗口结束合成一批。

结果就是文档里定的那条：连发 5 条 = 一次理解、一次回复。
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from .message import Message

BatchHandler = Callable[[list[Message]], Awaitable[None]]


class Merger:
    def __init__(self, window_ms: int, on_batch: BatchHandler):
        self.window = max(0, window_ms) / 1000.0
        self._buf: dict[str, list[Message]] = {}
        self._timers: dict[str, asyncio.Task] = {}
        self._on_batch = on_batch

    def feed(self, msg: Message) -> None:
        conv = msg.conv_id
        self._buf.setdefault(conv, []).append(msg)

        old = self._timers.pop(conv, None)
        if old is not None:
            old.cancel()

        if self.window <= 0:
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
        if batch:
            await self._on_batch(batch)

    async def close(self) -> None:
        for task in list(self._timers.values()):
            task.cancel()
        self._timers.clear()
        self._buf.clear()