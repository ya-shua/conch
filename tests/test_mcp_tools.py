"""工具只摆她用得上的那几个：白名单之外的一律不给她看。"""

from __future__ import annotations

import asyncio

from core.mcp import McpPool, McpServer


def fake_server(name: str, tools: list[str], **spec) -> McpServer:
    srv = McpServer({"name": name, "command": "x", **spec})
    srv._tools = [{"name": t, "description": t, "inputSchema": {"type": "object"}} for t in tools]
    return srv


def pool_of(*servers: McpServer) -> McpPool:
    pool = McpPool([])
    for srv in servers:
        pool.servers[srv.name] = srv
    return pool


def names(schemas) -> list[str]:
    return [s["function"]["name"] for s in schemas]


ALL = ["a1", "a2", "a3"]


def test_only_limits_what_she_sees():
    srv = fake_server("a", ALL, only=["a1", "a2"])
    assert names(pool_of(srv).tools_for("owner", "随便聊聊")) == ["a1", "a2"]


def test_empty_only_hides_everything():
    """声来就是这样：16 个全藏起来，她只看见海螺给的 voice_say。"""
    srv = fake_server("a", ALL, only=[])
    assert names(pool_of(srv).tools_for("owner", "说句话")) == []


def test_no_only_key_means_all():
    srv = fake_server("a", ALL)
    assert names(pool_of(srv).tools_for("owner", "")) == ["a1", "a2", "a3"]


def test_scope_still_respected():
    srv = fake_server("a", ALL, only=["a1"], scope="owner")
    assert names(pool_of(srv).tools_for("group", "hi")) == []


def test_hidden_tool_can_still_be_called_by_name():
    """藏起来只是不摆到她面前；插件自己（call_raw / call）照样能调。"""
    srv = fake_server("a", ALL, only=[])
    called: list[str] = []

    async def fake_ensure() -> None:
        return None

    async def fake_call(tool, args):
        called.append(tool)
        return "ok"

    srv.ensure = fake_ensure  # type: ignore[assignment]
    srv.call = fake_call  # type: ignore[assignment]
    out = asyncio.run(pool_of(srv).call("a3", {}))
    assert called == ["a3"] and out == "ok"