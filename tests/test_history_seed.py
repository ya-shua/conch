"""重启后把最近的话接回来：群/私聊格式对、当前消息不重复、太旧的不捡。"""

from __future__ import annotations

import importlib.util
import time
from pathlib import Path

import pytest

from _paths import conch_main

PLUGIN = conch_main()
OWNER = "1234567890"
GROUP = "group:123456"
PRIVATE = f"private:{OWNER}"


@pytest.fixture()
def plugin():
    spec = importlib.util.spec_from_file_location("conch_plugin_seed", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._HISTORY.clear()
    return mod


class FakeStore:
    def __init__(self, rows):
        self.rows = rows  # 新的在前（跟真库一样）

    def message_list(self, conv_id=None, limit=80, before_id=None):
        return [dict(r) for r in self.rows[:limit]]


class Log:
    def __init__(self):
        self.lines = []

    def info(self, fmt, *a):
        self.lines.append(fmt % a if a else fmt)

    def warning(self, fmt, *a):
        self.lines.append("WARN " + (fmt % a if a else fmt))


def ctx_with(rows):
    import types

    return types.SimpleNamespace(store=FakeStore(rows), log=Log())


def row(conv, direction, text, *, sender="", name="", mid="", ts=None):
    return {
        "conv_id": conv,
        "direction": direction,
        "text": text,
        "sender_id": sender,
        "sender_name": name,
        "message_id": mid,
        "ts": int((ts if ts is not None else time.time()) * 1000),
    }


def test_group_history_uses_group_number_and_qq(plugin):
    rows = [
        row(GROUP, "in", "别理她", sender=OWNER, name="管理员", mid="9", ts=time.time() - 10),
        row(GROUP, "out", "知道啦", mid="8", ts=time.time() - 20),
        row(GROUP, "in", "在吗", sender="120000", name="老墨", mid="7", ts=time.time() - 30),
    ]
    ctx = ctx_with(rows)
    plugin.seed_history(ctx, GROUP, {"9"}, OWNER)
    got = plugin.recent_turns(GROUP)
    assert [r["role"] for r in got] == ["user", "assistant"]
    assert got[0]["content"] == "[群123456] 老墨 #120000: 在吗"
    assert got[1]["content"] == "知道啦"
    # 当前这条（mid=9）不该被重复接进来
    assert all("别理她" not in r["content"] for r in got)


def test_private_history_marks_admin_with_qq(plugin):
    rows = [
        row(PRIVATE, "in", "肥鱼", sender=OWNER, name="管理员", mid="5", ts=time.time() - 10),
        row(PRIVATE, "out", "嗯？", mid="4", ts=time.time() - 20),
    ]
    plugin.seed_history(ctx_with(rows), PRIVATE, set(), OWNER)
    got = plugin.recent_turns(PRIVATE)
    # 时间顺序：她的「嗯？」在前，你的「肥鱼」在后
    assert got[0]["content"] == "嗯？"
    assert got[1]["content"] == f"[私聊] 管理员 #{OWNER}: 肥鱼"


def test_seed_respects_same_limits_as_memory(plugin):
    rows = [row(GROUP, "in", "x" * 300, sender="120000", name="老墨", mid=str(i)) for i in range(30)]
    plugin.seed_history(ctx_with(rows), GROUP, set(), OWNER)
    got = plugin.recent_turns(GROUP)
    assert len(got) <= plugin._HISTORY_KEEP
    assert sum(len(r["content"]) for r in got) <= plugin._HISTORY_CHARS + 400


def test_old_history_is_not_seeded(plugin):
    old = time.time() - 25 * 3600
    rows = [row(PRIVATE, "in", "昨晚的话", sender=OWNER, name="牙刷", mid="1", ts=old)]
    plugin.seed_history(ctx_with(rows), PRIVATE, set(), OWNER)
    assert plugin.recent_turns(PRIVATE) == []


def test_existing_memory_is_left_alone(plugin):
    plugin.remember_turn(PRIVATE, "user", "我刚说的")
    rows = [row(PRIVATE, "in", "库里的话", sender=OWNER, name="牙刷", mid="1")]
    plugin.seed_history(ctx_with(rows), PRIVATE, set(), OWNER)
    assert [r["content"] for r in plugin.recent_turns(PRIVATE)] == ["我刚说的"]