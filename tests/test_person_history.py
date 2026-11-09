"""同一个人的两个号，私聊共用一个上下文（按称呼卡），别人各管各的。"""
from __future__ import annotations

import importlib.util
import time

import pytest

from _paths import conch_main

PLUGIN = conch_main()
A = "1234567890"
B = "9876543210"


@pytest.fixture()
def plugin():
    spec = importlib.util.spec_from_file_location("conch_plugin_person_hist", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._HISTORY.clear()
    return mod


class Log:
    def info(self, fmt, *a): pass
    def warning(self, fmt, *a): pass
    def debug(self, fmt, *a): pass


def ctx_of(cards, rows=None):
    import types

    cfg = types.SimpleNamespace(admins=cards, owner_ids=[c["ids"][0] for c in cards],
                                owner_qq=cards[0]["ids"][0] if cards else "")

    class Store:
        def message_list(self, conv_id=None, limit=80, before_id=None):
            return [r for r in (rows or []) if r.get("conv_id") == conv_id][:limit]

    return types.SimpleNamespace(cfg=cfg, store=Store(), log=Log())


CARDS = [
    {"name": "管理员A", "ids": [A, B], "private": "老爹", "group": "", "by_group": {}},
]


def test_two_ids_share_one_history_key(plugin):
    ctx = ctx_of(CARDS)
    k1 = plugin.history_key(ctx.cfg, f"private:{A}")
    k2 = plugin.history_key(ctx.cfg, f"private:{B}")
    assert k1 == k2 == "person:管理员A"
    # 群聊和陌生人私聊各管各的
    assert plugin.history_key(ctx.cfg, "group:123456") == "group:123456"
    assert plugin.history_key(ctx.cfg, "private:999999") == "private:999999"  # privacy-ok: 假号


def test_history_written_on_one_id_is_visible_on_the_other(plugin):
    ctx = ctx_of(CARDS)
    k = plugin.history_key(ctx.cfg, f"private:{A}")
    plugin.remember_turn(k, "user", "[私聊] 管理员A #1234567890: 今晚吃什么")
    plugin.remember_turn(k, "assistant", "随便，别又是泡面")
    got = plugin.recent_turns(plugin.history_key(ctx.cfg, f"private:{B}"))
    assert [r["role"] for r in got] == ["user", "assistant"]


def test_seed_merges_both_ids(plugin):
    now = time.time()
    rows = [
        {"conv_id": f"private:{A}", "direction": "in", "text": "在吗",
         "sender_id": A, "sender_name": "管理员", "message_id": "2",
         "ts": int((now - 10) * 1000)},
        {"conv_id": f"private:{B}", "direction": "in", "text": "刚用另一个号聊过",
         "sender_id": B, "sender_name": "我", "message_id": "1",
         "ts": int((now - 20) * 1000)},
    ]
    ctx = ctx_of(CARDS, rows)
    plugin.seed_history(ctx, f"private:{A}", set(), CARDS, siblings=[f"private:{A}", f"private:{B}"])
    got = plugin.recent_turns("person:管理员A")
    assert len(got) == 2
    # 时间顺序：B 号的在前，A 号的在后；两个号的标签都是卡名，只有号码不同
    assert "管理员A #9876543210" in got[0]["content"]
    assert "管理员A #1234567890" in got[1]["content"]
