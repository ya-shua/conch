"""插件注册工具的地方。

模型能调的工具有两种：
  - MCP 那批（桌面上的独立程序，conch 插件自己接）
  - 插件自己实现的（比如「联网」插件），通过这里注册

插件在 setup(ctx) 里 `ctx.services["tools"].register(spec, handler, owner="websearch")`，
teardown 时 `unregister_owner("websearch")`。同名工具后注册的覆盖前一个。
"""
from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable

Handler = Callable[[dict], Awaitable[str] | str]


class ToolBox:
    def __init__(self) -> None:
        self._specs: dict[str, dict[str, Any]] = {}
        self._handlers: dict[str, Handler] = {}
        self._owners: dict[str, str] = {}

    # ---------- 注册 ----------
    def register(self, spec: dict[str, Any], handler: Handler, owner: str = "") -> str:
        fn = (spec or {}).get("function") or {}
        name = str(fn.get("name") or "").strip()
        if not name:
            raise ValueError("工具要有名字")
        self._specs[name] = spec
        self._handlers[name] = handler
        self._owners[name] = owner or str(fn.get("name"))
        return name

    def unregister_owner(self, owner: str) -> int:
        names = [n for n, o in self._owners.items() if o == owner]
        for n in names:
            self._specs.pop(n, None)
            self._handlers.pop(n, None)
            self._owners.pop(n, None)
        return len(names)

    # ---------- 给模型看 / 用 ----------
    def specs(self) -> list[dict[str, Any]]:
        return list(self._specs.values())

    def names(self) -> list[str]:
        return list(self._specs)

    async def call(self, name: str, args: dict[str, Any]) -> str | None:
        """不是这儿的工具就返回 None，让调用方换别的地方找。"""
        handler = self._handlers.get(name)
        if handler is None:
            return None
        try:
            result = handler(args if isinstance(args, dict) else {})
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            return f"工具 {name} 出错：{str(exc)[:200]}"
        if isinstance(result, (dict, list)):
            import json

            return json.dumps(result, ensure_ascii=False)
        return str(result)