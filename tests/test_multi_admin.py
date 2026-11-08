"""多个管理员：两张卡（两个人）分得清；一张卡挂两个号（一个人）认得成同一个。

群里的真群管/群主不会跟管理员搞混。
"""

from __future__ import annotations

import asyncio
import importlib.util

import pytest

from _paths import conch_main

PLUGIN = conch_main()
A = "1234567890"
B = "9876543210"
GROUP = "group:123456"
BOTH = [A, B]

# 多人多号：两张卡，两个人
TWO_PEOPLE = [
    {"name": "管理员A", "ids": [A], "private": "老爹", "group": "老大"},
    {"name": "管理员B", "ids": [B]},
]
# 单人多号：一张卡，两个号
ONE_PERSON = [{"name": "管理员A", "ids": BOTH, "private": "老爹", "group": "老大"}]


@pytest.fixture()
def plugin():
    spec = importlib.util.spec_from_file_location("conch_plugin_multi_admin", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._ROLES.clear()
    return mod


class Client:
    def __init__(self, roles=None):
        self.roles = roles or {}

    async def call(self, action, payload):
        return {"role": self.roles.get(str(payload["user_id"]), "member")}


class Log:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def debug(self, *a, **k): pass


def gmsg(plugin, uid, name, text="在吗"):
    return plugin.Message(
        conv_id=GROUP, kind="group", sender_id=uid, sender_name=name,
        message_id="1", segments=plugin.make_text(text), ts=0,
    )


def pmsg(plugin, uid, name, text="在吗"):
    return plugin.Message(
        conv_id=f"private:{uid}", kind="private", sender_id=uid, sender_name=name,
        message_id="1", segments=plugin.make_text(text), ts=0,
    )


def ctx_of(client, cards=None):
    import types

    cfg = types.SimpleNamespace(admins=[], owner_ids=[], owner_qq="")
    return types.SimpleNamespace(cfg=cfg if cards is None else _Cfg(cards), services={"onebot": client}, log=Log())


class _Cfg:
    """假装成 Config：plugin 只看 admins / owner_ids / owner_qq。"""

    def __init__(self, cards):
        self.admins = [_Card(c) for c in cards]
        ids = []
        for c in cards:
            for uid in c["ids"]:
                ids.append(uid)
        self.owner_ids = ids
        self.owner_qq = ids[0] if ids else ""


class _Card:
    def __init__(self, d):
        self.name = d.get("name", "管理员")
        self.ids = list(d.get("ids") or [])
        self.private_call = d.get("private", "")
        self.group_call = d.get("group", "")
        self.by_group = dict(d.get("by_group") or {})


def test_two_people_are_distinguishable(plugin):
    a = gmsg(plugin, A, "管理员")
    b = gmsg(plugin, B, "我")
    lines = plugin.render_batch([a, b], TWO_PEOPLE).splitlines()
    assert lines[0] == f"[群123456] 管理员A #{A}: 在吗"
    assert lines[1] == f"[群123456] 管理员B #{B}: 在吗"


def test_one_person_two_ids_same_name(plugin):
    """单人多号：两个号的标签卡名一样，只有号码不同。"""
    x = gmsg(plugin, A, "管理员")
    y = gmsg(plugin, B, "我")
    lines = plugin.render_batch([x, y], ONE_PERSON).splitlines()
    assert lines[0] == f"[群123456] 管理员A #{A}: 在吗"
    assert lines[1] == f"[群123456] 管理员A #{B}: 在吗"


def test_legacy_single_admin_also_carries_number(plugin):
    """老配置（只有号码、没建卡）：也叫「管理员」，一样带号码。"""
    m = gmsg(plugin, A, "管理员")
    assert plugin.render_batch([m], [A]) == f"[群123456] 管理员 #{A}: 在吗"


def test_group_admin_is_not_admin(plugin):
    m = gmsg(plugin, "300000", "老墨")
    asyncio.run(plugin.ensure_member_role(ctx_of(Client({"300000": "admin"})), m))
    assert plugin.render_batch([m], TWO_PEOPLE) == "[群123456] 群管 老墨 #300000: 在吗"


def test_group_owner_is_not_admin(plugin):
    m = gmsg(plugin, "300000", "老墨")
    asyncio.run(plugin.ensure_member_role(ctx_of(Client({"300000": "owner"})), m))
    assert plugin.render_batch([m], TWO_PEOPLE) == "[群123456] 群主 老墨 #300000: 在吗"


def test_both_admins_can_command_in_private(plugin):
    a = pmsg(plugin, A, "管理员")
    b = pmsg(plugin, B, "我")
    cfg = _Cfg(TWO_PEOPLE)
    assert plugin.is_owner_private(a, cfg)
    assert plugin.is_owner_private(b, cfg)
    assert not plugin.is_owner_private(pmsg(plugin, "999", "路人"), cfg)


def test_private_call_hint(plugin):
    ctx = ctx_of(Client(), TWO_PEOPLE)
    line = plugin.call_hint(ctx, pmsg(plugin, A, "管理员"))
    assert line == "\n\n【叫法】管理员A 希望你叫他「老爹」；不想这么叫也没关系。"
    # B 没配称呼 → 一个字都不加
    assert plugin.call_hint(ctx, pmsg(plugin, B, "我")) == ""


def test_group_call_hint_and_by_group_override(plugin):
    ctx = ctx_of(Client(), TWO_PEOPLE)
    line = plugin.call_hint(ctx, gmsg(plugin, A, "管理员"))
    assert line == "\n\n【叫法】管理员A 希望你叫他「老大」；不想这么叫也没关系。"

    cards = [{"name": "管理员A", "ids": [A], "group": "老大",
              "by_group": {"123456": "老翰"}}]
    ctx2 = ctx_of(Client(), cards)
    assert plugin.call_hint(ctx2, gmsg(plugin, A, "管理员")) == "\n\n【叫法】管理员A 希望你叫他「老翰」；不想这么叫也没关系。"


def test_call_hint_never_for_strangers(plugin):
    ctx = ctx_of(Client(), TWO_PEOPLE)
    assert plugin.call_hint(ctx, gmsg(plugin, "999", "路人")) == ""


def test_owner_list_parsing(plugin):
    assert plugin._owner_ids("1234567890，9876543210") == [A, B]
    assert plugin._owner_ids("1234567890,9876543210,1234567890") == [A, B]
    assert plugin._owner_ids("") == []

def test_multiple_calls_are_listed(plugin):
    """一个字段填多个称呼：她随便挑一个叫，也可以不叫。"""
    cards = [{"name": "管理员A", "ids": [A], "group": "老大，阿大、大老板"}]
    ctx = ctx_of(Client(), cards)
    line = plugin.call_hint(ctx, gmsg(plugin, A, "管理员"))
    assert "「老大」" in line and "「阿大」" in line and "「大老板」" in line
    assert "不想这么叫也没关系" in line
