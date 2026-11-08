"""记忆库写入：跟她在聊天里看到的一致 —— 带「在哪」和 QQ 号，管理员只写管理员。"""

from __future__ import annotations

import asyncio
import importlib.util
import types

import pytest

from _paths import conch_main

PLUGIN = conch_main()
OWNER = "1234567890"


@pytest.fixture()
def plugin():
    spec = importlib.util.spec_from_file_location("conch_plugin_ingest", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakePool:
    def __init__(self):
        self.calls = []

    async def call_raw(self, server, tool, payload):
        self.calls.append((server, tool, dict(payload)))
        return "ok"


class Log:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass


def make_ctx(plugin, pool, ingest=True):
    return types.SimpleNamespace(
        cfg=types.SimpleNamespace(owner_qq=OWNER, memory_ingest_enabled=ingest),
        services={"mcppool": pool},
        log=Log(),
    )


def msg(plugin, conv, sender, name, text):
    return plugin.Message(
        conv_id=conv,
        kind=conv.split(":")[0],
        sender_id=sender,
        sender_name=name,
        message_id="1",
        segments=plugin.make_text(text),
        ts=0,
    )


def test_group_line_has_group_number_and_qq(plugin):
    pool = FakePool()
    m = msg(plugin, "group:123456", "120000", "老墨", "在吗")
    asyncio.run(plugin.remember_into_ring(make_ctx(plugin, pool), m))
    _, tool, payload = pool.calls[0]
    assert tool == "ring_ingest"
    assert payload["content"] == "[群123456] 老墨 #120000：在吗"
    assert payload["speaker"] == "老墨 #120000"
    assert payload["source"] == "group"


def test_group_admin_is_just_admin(plugin):
    pool = FakePool()
    m = msg(plugin, "group:123456", OWNER, "管理员", "别理她")
    asyncio.run(plugin.remember_into_ring(make_ctx(plugin, pool), m))
    payload = pool.calls[0][2]
    assert payload["content"] == "[群123456] 管理员 #1234567890：别理她"
    assert payload["speaker"] == "管理员"


def test_private_line_is_tagged_and_keeps_qq(plugin):
    pool = FakePool()
    m = msg(plugin, f"private:{OWNER}", OWNER, "管理员", "肥鱼")
    asyncio.run(plugin.remember_into_ring(make_ctx(plugin, pool), m))
    payload = pool.calls[0][2]
    assert payload["content"] == f"[私聊] 管理员 #{OWNER}：肥鱼"
    assert payload["source"] == "private"


def test_ingest_off_writes_nothing(plugin):
    pool = FakePool()
    m = msg(plugin, "group:123456", "120000", "老墨", "在吗")
    asyncio.run(plugin.remember_into_ring(make_ctx(plugin, pool, ingest=False), m))
    assert pool.calls == []