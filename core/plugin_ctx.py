"""插件上下文：插件能拿到的全部能力，都从这里出。"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from . import log
from .config import Config
from .message import Segment
from .pricing import estimate_cost
from .store import Store

Handler = Callable[[Any], Awaitable[Any] | Any]


class Ledger:
    """计量账本。插件只能用自己 id 记账，查全局要显式传 scope。"""

    def __init__(self, store: Store, plugin_id: str):
        self._store = store
        self._plugin_id = plugin_id

    def record(
        self,
        *,
        feature: str,
        conv_id: str,
        turn_id: str,
        model: str,
        usage: dict[str, int],
        step: int = 0,
        retry: int = 0,
        ts: int | None = None,
    ) -> dict[str, Any]:
        ts_ms = ts or int(time.time() * 1000)
        cost, peak, known = estimate_cost(
            model,
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("cached_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
            ts_ms,
        )
        row = {
            "ts": ts_ms,
            "plugin_id": self._plugin_id,
            "feature": feature,
            "conv_id": conv_id,
            "turn_id": turn_id,
            "step": step,
            "retry": retry,
            "model": model,
            "prompt_tokens": usage.get("prompt_tokens") or 0,
            "cached_tokens": usage.get("cached_tokens") or 0,
            "completion_tokens": usage.get("completion_tokens") or 0,
            "cost_yuan": cost,
            "peak": peak,
        }
        self._store.usage_add(row)
        return {**row, "estimated": not known}

    def summary(self, since_ms: int, scope: str = "self") -> list[dict[str, Any]]:
        plugin_id = self._plugin_id if scope == "self" else None
        return self._store.usage_summary(since_ms, plugin_id=plugin_id)


class PluginContext:
    def __init__(
        self,
        *,
        meta: Any,
        cfg: Config,
        bus: Any,
        store: Store,
        services: dict[str, Any],
    ):
        self.meta = meta
        self.id: str = meta.id
        self.config: dict[str, Any] = dict(meta.config)
        self.cfg = cfg
        self.bus = bus
        self.store = store
        self.services = services
        self.log = log.get(f"plugin.{meta.id}")
        self.ledger = Ledger(store, meta.id)

    def on(self, event: str, handler: Handler, *, priority: int = 100) -> None:
        self.bus.on(event, handler, priority=priority, owner=self.id)

    async def send(self, conv_id: str, segments: list[Segment]) -> None:
        client = self.services.get("onebot")
        if client is None:
            raise RuntimeError("OneBot 客户端还没就绪")
        await client.send(conv_id, segments)