"""事件总线：按优先级串行派发，单个 handler 崩了不拖累后面的。"""

from __future__ import annotations

import inspect
import itertools
from collections import defaultdict
from typing import Any, Awaitable, Callable

from . import log

Handler = Callable[[Any], Awaitable[Any] | Any]

CONSUME = "consume"


class Bus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[tuple[int, int, Handler, str]]] = defaultdict(list)
        self._seq = itertools.count()
        self._log = log.get("bus")

    def on(self, name: str, handler: Handler, *, priority: int = 100, owner: str = "") -> None:
        self._handlers[name].append((priority, next(self._seq), handler, owner))
        self._handlers[name].sort(key=lambda item: (item[0], item[1]))

    def off_owner(self, owner: str) -> None:
        for name in list(self._handlers):
            self._handlers[name] = [h for h in self._handlers[name] if h[3] != owner]
            if not self._handlers[name]:
                del self._handlers[name]

    async def emit(self, name: str, payload: Any = None) -> bool:
        """返回 True 表示被某个 handler consume，后续 handler 不再执行。"""
        for _prio, _seq, handler, owner in list(self._handlers.get(name, ())):
            try:
                result = handler(payload)
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:  # 一个插件坏了不能拖垮管道
                self._log.exception("handler 异常 name=%s owner=%s err=%s", name, owner, exc)
                continue
            if result == CONSUME:
                return True
        return False

    def count(self, name: str) -> int:
        return len(self._handlers.get(name, ()))