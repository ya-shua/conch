"""记忆库写入：跟她在聊天里看到的一致 —— 带「在哪」和 QQ 号，管理员只写管理员。

槽位（conv_key）必须按聊天分开：所有私聊共用一个槽，张三和李四的话会被拼成同一条记忆。
speaker 只认 user / self：卡名不是 speaker，self 才有 3 倍权重。
"""

from __future__ import annotations

import asyncio
import types
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


def msg(plugin, conv, sender, name, text, ts=1700000000, mid="1"):
    return plugin.Message(
        conv_id=conv,
        kind=conv.split(":")[0],
        sender_id=sender,
        sender_name=name,
        message_id=mid,
        segments=plugin.make_text(text),
        ts=ts,
    )


def test_group_line_has_group_number_and_qq(plugin):
    pool = FakePool()
    m = msg(plugin, "group:123456", "120000", "老墨", "在吗")
    asyncio.run(plugin.remember_into_ring(make_ctx(plugin, pool), m))
    _, tool, payload = pool.calls[0]
    assert tool == "ring_ingest"
    assert payload["content"] == "[群123456] 老墨 #120000：在吗"
    assert payload["speaker"] == "user"
    assert payload["source"] == "group"
    assert payload["conv_key"] == "group:123456"
    assert payload["message_id"] == "1"
    assert payload["at"] == 1700000000 * 1000


def test_group_admin_is_just_admin(plugin):
    pool = FakePool()
    m = msg(plugin, "group:123456", OWNER, "管理员", "别理她")
    asyncio.run(plugin.remember_into_ring(make_ctx(plugin, pool), m))
    payload = pool.calls[0][2]
    assert payload["content"] == "[群123456] 管理员 #1234567890：别理她"
    assert payload["speaker"] == "user"


def test_private_line_is_tagged_and_keeps_qq(plugin):
    pool = FakePool()
    m = msg(plugin, f"private:{OWNER}", OWNER, "管理员", "肥鱼")
    asyncio.run(plugin.remember_into_ring(make_ctx(plugin, pool), m))
    payload = pool.calls[0][2]
    assert payload["content"] == f"[私聊] 管理员 #{OWNER}：肥鱼"
    assert payload["source"] == "private"
    assert payload["conv_key"] == "private:card:管理员"  # 有卡就按卡，不按号


def test_two_private_chats_do_not_share_a_slot(plugin):
    pool = FakePool()
    ctx = make_ctx(plugin, pool)
    asyncio.run(plugin.remember_into_ring(ctx, msg(plugin, "private:111", "111", "张三", "在吗")))
    asyncio.run(plugin.remember_into_ring(ctx, msg(plugin, "private:222", "222", "李四", "在吗")))
    a, b = pool.calls[0][2], pool.calls[1][2]
    assert a["conv_key"] == "private:111"
    assert b["conv_key"] == "private:222"


def test_two_groups_do_not_share_a_slot(plugin):
    pool = FakePool()
    ctx = make_ctx(plugin, pool)
    asyncio.run(plugin.remember_into_ring(ctx, msg(plugin, "group:100", "1", "A", "一")))
    asyncio.run(plugin.remember_into_ring(ctx, msg(plugin, "group:200", "2", "B", "二")))
    assert pool.calls[0][2]["conv_key"] == "group:100"
    assert pool.calls[1][2]["conv_key"] == "group:200"


def test_her_own_words_are_ingested_as_self(plugin):
    pool = FakePool()
    asyncio.run(plugin.remember_own(make_ctx(plugin, pool), "group:123456", "我在呢\n马上来", "turn-7"))
    _, tool, payload = pool.calls[0]
    assert tool == "ring_ingest"
    assert payload["speaker"] == "self"
    assert payload["conv_key"] == "group:123456"
    assert payload["content"] == "[群123456] 我在呢\n马上来"
    assert payload["message_id"].startswith("out:")


def test_own_private_message_uses_private_slot(plugin):
    pool = FakePool()
    asyncio.run(plugin.remember_own(make_ctx(plugin, pool), f"private:{OWNER}", "嗯", "turn-8"))
    payload = pool.calls[0][2]
    assert payload["conv_key"] == f"private:{OWNER}"
    assert payload["content"] == "[私聊] 嗯"


def test_voice_without_transcription_writes_nothing(plugin):
    pool = FakePool()
    m = plugin.Message(
        conv_id="group:123456",
        kind="group",
        sender_id="120000",
        sender_name="老墨",
        message_id="9",
        segments=[plugin.Segment("record", {"file": "a.amr"})],
        ts=1700000000,
    )
    asyncio.run(plugin.remember_into_ring(make_ctx(plugin, pool), m))
    assert pool.calls == []  # 没转出来就当没听见


def test_voice_transcription_goes_into_content_and_note(plugin):
    pool = FakePool()
    m = plugin.Message(
        conv_id="group:123456",
        kind="group",
        sender_id="120000",
        sender_name="老墨",
        message_id="9",
        segments=[plugin.Segment("record", {"file": "a.amr"})],
        ts=1700000000,
    )
    asyncio.run(plugin.remember_into_ring(make_ctx(plugin, pool), m, heard="今天下雨"))
    payload = pool.calls[0][2]
    assert payload["content"] == "[群123456] 老墨 #120000：[语音转写] 今天下雨"
    assert payload["note"] == "语音转写，可能不准"
    assert payload["conv_key"] == "group:123456"


def test_voice_text_is_detected_from_prefixed_text(plugin):
    assert plugin._voice_text_of(plugin.Message(
        conv_id="private:1", kind="private", sender_id="1", sender_name="谁",
        message_id="1", segments=[plugin.Segment("text", {"text": plugin.VOICE_MARK + "你好"})], ts=0,
    )) == "你好"
    assert plugin._voice_text_of(plugin.Message(
        conv_id="private:1", kind="private", sender_id="1", sender_name="谁",
        message_id="1", segments=[plugin.Segment("text", {"text": "普通文字"})], ts=0,
    )) == ""


def test_ingest_off_writes_nothing(plugin):
    pool = FakePool()
    m = msg(plugin, "group:123456", "120000", "老墨", "在吗")
    asyncio.run(plugin.remember_into_ring(make_ctx(plugin, pool, ingest=False), m))
    assert pool.calls == []


def test_ingest_off_also_blocks_own(plugin):
    pool = FakePool()
    asyncio.run(plugin.remember_own(make_ctx(plugin, pool, ingest=False), "private:1", "嗯"))
    assert pool.calls == []


def make_ctx_cards(plugin, pool, admins):
    cfg = types.SimpleNamespace(owner_qq=OWNER, memory_ingest_enabled=True, admins=admins)
    return types.SimpleNamespace(cfg=cfg, services={"mcppool": pool}, log=Log())


def test_one_card_two_numbers_share_a_private_slot(plugin):
    pool = FakePool()
    admins = [{"id": "c1", "name": "老爹", "ids": [OWNER, "20001"], "call": {}}]  # privacy-ok 假号
    ctx = make_ctx_cards(plugin, pool, admins)
    asyncio.run(plugin.remember_into_ring(ctx, msg(plugin, f"private:{OWNER}", OWNER, "老爹", "大号")))
    asyncio.run(plugin.remember_into_ring(ctx, msg(plugin, "private:20001", "20001", "老爹", "小号")))  # privacy-ok 假号
    a, b = pool.calls[0][2], pool.calls[1][2]
    assert a["conv_key"] == "private:card:老爹"
    assert b["conv_key"] == "private:card:老爹"  # 一卡多号 = 同一个人
    assert a["content"].endswith(f"#{OWNER}：大号")


def test_strangers_do_not_share_a_private_slot(plugin):
    pool = FakePool()
    ctx = make_ctx_cards(plugin, pool, [])
    asyncio.run(plugin.remember_into_ring(ctx, msg(plugin, "private:111", "111", "张三", "在吗")))
    asyncio.run(plugin.remember_into_ring(ctx, msg(plugin, "private:222", "222", "李四", "在吗")))
    assert pool.calls[0][2]["conv_key"] == "private:111"
    assert pool.calls[1][2]["conv_key"] == "private:222"


def test_her_reply_goes_into_the_same_card_slot(plugin):
    pool = FakePool()
    admins = [{"id": "c1", "name": "老爹", "ids": [OWNER], "call": {}}]
    ctx = make_ctx_cards(plugin, pool, admins)
    who = plugin._card_who(ctx, OWNER)
    asyncio.run(plugin.remember_own(ctx, f"private:{OWNER}", "嗯", "turn-1", who))
    payload = pool.calls[0][2]
    assert payload["conv_key"] == "private:card:老爹"
    assert payload["speaker"] == "self"


def test_silk_magic_is_recognised_with_prefix_byte(plugin):
    assert plugin._is_silk(b"\x02#!SILK_V3" + b"\x00" * 20)
    assert plugin._is_silk(b"#!SILK_V3" + b"\x00" * 20)
    assert not plugin._is_silk(b"ID3\x03\x00")


def test_voice_source_prefers_local_file_then_url_then_id(plugin, tmp_path):
    def mk(data):
        return plugin.Message(
            conv_id="private:1", kind="private", sender_id="1", sender_name="谁",
            message_id="1", segments=[plugin.Segment("record", data)], ts=0,
        )

    real = tmp_path / "a.silk"
    real.write_bytes(b"#!SILK_V3")
    assert plugin._voice_source(mk({"file": str(real)})) == ("file", str(real))
    assert plugin._voice_source(mk({"file": "x.silk", "url": "https://e/x"})) == ("url", "https://e/x")
    assert plugin._voice_source(mk({"file": "x.silk"})) == ("id", "x.silk")


def test_render_batch_keeps_voice_mark_short(plugin):
    """上下文不追提醒：只在正文带短标记，规则放人设提示里。"""
    voice = plugin.Message(
        conv_id="group:1", kind="group", sender_id="1", sender_name="老墨", message_id="1",
        segments=[plugin.Segment("text", {"text": plugin.VOICE_MARK + "今天下雨"})], ts=0,
    )
    text = plugin.render_batch([voice])
    assert text == "[群1] 老墨 #1: [语音转写] 今天下雨"
    assert len(text.splitlines()) == 1


def test_voice_rule_is_in_prompt_not_in_context(plugin, tmp_path):
    class Cfg:
        roles_dir = str(tmp_path)
        raw = {}

    ctx = types.SimpleNamespace(cfg=Cfg(), log=FakeLog())
    prompt = plugin.system_prompt(ctx)
    assert "语音转写" in prompt
    assert "按对的理解" in prompt or "别把错字当原话" in prompt


class FakeLog:
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
