"""群里有多个人是管理员：真职位照实标，但「管理员」永远只指主人。"""

from __future__ import annotations

import asyncio
import importlib.util

import pytest

from _paths import conch_main

PLUGIN = conch_main()
OWNER = "1234567890"
GROUP = "group:123456"


@pytest.fixture()
def plugin():
    spec = importlib.util.spec_from_file_location("conch_plugin_roles", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._ROLES.clear()
    return mod


class FakeClient:
    def __init__(self, roles):
        self.roles = roles  # uid -> role
        self.calls = []

    async def call(self, action, payload):
        self.calls.append((action, dict(payload)))
        if action != "get_group_member_info":
            return {}
        return {"role": self.roles.get(str(payload["user_id"]), "member")}


class Log:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass


def make(plugin, uid, name, text="在吗"):
    return plugin.Message(
        conv_id=GROUP, kind="group", sender_id=uid, sender_name=name,
        message_id="1", segments=plugin.make_text(text), ts=0,
    )


def ctx_with(client):
    import types

    return types.SimpleNamespace(services={"onebot": client}, log=Log())


def test_group_owner_is_labelled_owner(plugin):
    m = make(plugin, "111222333", "老墨")
    asyncio.run(plugin.ensure_member_role(ctx_with(FakeClient({"111222333": "owner"})), m))
    assert plugin.render_batch([m], OWNER) == "[群123456] 群主 老墨 #111222333: 在吗"


def test_multiple_admins_all_labelled(plugin):
    client = FakeClient({"111": "admin", "222": "admin", "333": "owner"})
    msgs = [make(plugin, "111", "甲"), make(plugin, "222", "乙"), make(plugin, "333", "丙")]
    for m in msgs:
        asyncio.run(plugin.ensure_member_role(ctx_with(client), m))
    lines = plugin.render_batch(msgs, OWNER).splitlines()
    assert lines[0] == "[群123456] 群管 甲 #111: 在吗"
    assert lines[1] == "[群123456] 群管 乙 #222: 在吗"
    assert lines[2] == "[群123456] 群主 丙 #333: 在吗"


def test_owner_stays_admin_even_when_plain_member(plugin):
    """爸在群里可能只是普通群友 —— 照样叫「管理员」，跟职位无关。"""
    m = make(plugin, OWNER, "管理员")
    asyncio.run(plugin.ensure_member_role(ctx_with(FakeClient({OWNER: "member"})), m))
    assert plugin.render_batch([m], OWNER) == "[群123456] 管理员 #1234567890: 在吗"


def test_role_is_cached_not_requeried(plugin):
    client = FakeClient({"111": "admin"})
    m = make(plugin, "111", "甲")
    asyncio.run(plugin.ensure_member_role(ctx_with(client), m))
    asyncio.run(plugin.ensure_member_role(ctx_with(client), m))
    assert len(client.calls) == 1


def test_lookup_failure_falls_back_to_plain_name(plugin):
    class Broken(FakeClient):
        async def call(self, action, payload):
            raise RuntimeError("接口没权限")

    m = make(plugin, "111", "甲")
    asyncio.run(plugin.ensure_member_role(ctx_with(Broken({})), m))
    assert plugin.render_batch([m], OWNER) == "[群123456] 甲 #111: 在吗"


def test_private_chat_never_queries_roles(plugin):
    class Broken(FakeClient):
        async def call(self, action, payload):
            raise AssertionError("私聊不该查群职位")

    m = plugin.Message(
        conv_id=f"private:{OWNER}", kind="private", sender_id=OWNER, sender_name="管理员",
        message_id="1", segments=plugin.make_text("在吗"), ts=0,
    )
    asyncio.run(plugin.ensure_member_role(ctx_with(Broken({})), m))