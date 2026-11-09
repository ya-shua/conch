"""一句一条：模型吐出来的多行，要拆成多条消息发出去。"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
from pathlib import Path

import pytest

from _paths import conch_main

PLUGIN = conch_main()


def load_plugin():
    spec = importlib.util.spec_from_file_location("conch_plugin_main", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeCtx:
    def __init__(self, stickers=None) -> None:
        self.sent: list[tuple[str, list]] = []
        self.services = {} if stickers is None else {"stickers": stickers}
        self.log = logging.getLogger("test.send_lines")

    async def send(self, conv_id, segments) -> None:
        self.sent.append((conv_id, segments))


class FakeStickers:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.asked: list[tuple[str, str]] = []

    async def pick_and_send(self, conv_id: str, hint: str = "") -> bool:
        self.asked.append((conv_id, hint))
        return self.ok


@pytest.fixture()
def plugin(monkeypatch):
    mod = load_plugin()

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", no_sleep)
    return mod


def test_multiline_reply_is_split(plugin):
    ctx = FakeCtx()
    asyncio.run(plugin.send_lines(ctx, "private:1", "谁生气了\n我就是无聊\n你哄我干嘛"))
    assert len(ctx.sent) == 3
    texts = [seg.data["text"] for _conv, segs in ctx.sent for seg in segs]
    assert texts == ["谁生气了", "我就是无聊", "你哄我干嘛"]


def test_blank_lines_dropped(plugin):
    ctx = FakeCtx()
    asyncio.run(plugin.send_lines(ctx, "private:1", "一\n\n  \n二"))
    assert len(ctx.sent) == 2


def test_single_line_still_one_message(plugin):
    ctx = FakeCtx()
    asyncio.run(plugin.send_lines(ctx, "private:1", "就一句"))
    assert len(ctx.sent) == 1
    assert ctx.sent[0][1][0].data["text"] == "就一句"


def test_sticker_line_becomes_a_real_sticker(plugin):
    stick = FakeStickers(ok=True)
    ctx = FakeCtx(stick)
    asyncio.run(plugin.send_lines(ctx, "private:1", "哼\n【表情包：猫猫-歪头】\n不理你了"))
    assert [seg.data["text"] for _c, segs in ctx.sent for seg in segs] == ["哼", "不理你了"]
    assert stick.asked == [("private:1", "猫猫-歪头")]


def test_missing_sticker_is_dropped_not_sent_as_text(plugin):
    stick = FakeStickers(ok=False)
    ctx = FakeCtx(stick)
    asyncio.run(plugin.send_lines(ctx, "private:1", "【表情包：不存在的名字】"))
    assert ctx.sent == []


def test_sticker_line_dropped_when_service_absent(plugin):
    ctx = FakeCtx()
    asyncio.run(plugin.send_lines(ctx, "private:1", "【表情：猫猫】"))
    assert ctx.sent == []


def test_inline_sticker_marker_stripped(plugin):
    ctx = FakeCtx()
    asyncio.run(plugin.send_lines(ctx, "private:1", "好啊【表情包：猫猫】走吧"))
    assert ctx.sent[0][1][0].data["text"] == "好啊走吧"


# ── CQ 码：好的翻译成表情段，写歪的一个字都不许漏到聊天窗口 ──

def test_valid_face_code_becomes_face_segment(plugin):
    segs = plugin.to_segments("笑死 [CQ:face,id=28]")
    assert [s.type for s in segs] == ["text", "face"]
    assert segs[1].data["id"] == "28"


def test_broken_face_code_is_dropped(plugin):
    # 这就是线上真出现过的：[CQ:face:undefined] 原样发到了 QQ
    assert plugin.to_segments("[CQ:face:undefined]") == []
    assert plugin.to_segments("[CQ:face:id=3]") == []


def test_face_code_with_non_numeric_id_dropped(plugin):
    assert plugin.to_segments("[CQ:face,id=abc]") == []


def test_unknown_code_types_dropped(plugin):
    assert plugin.to_segments("[CQ:image,file=x.png]") == []


def test_code_only_line_sends_nothing(plugin):
    ctx = FakeCtx()
    asyncio.run(plugin.send_lines(ctx, "private:1", "[CQ:face:undefined]"))
    assert ctx.sent == []


def test_junk_code_inside_sentence_keeps_the_words(plugin):
    ctx = FakeCtx()
    asyncio.run(plugin.send_lines(ctx, "private:1", "那叫青出于蓝 [CQ:face:undefined]"))
    assert [s.data["text"] for _c, segs in ctx.sent for s in segs] == ["那叫青出于蓝"]


# ── 括号里的心里话：她可以想，但别发到聊天窗口 ──

def test_parenthetical_aside_is_stripped(plugin):
    ctx = FakeCtx()
    asyncio.run(plugin.send_lines(ctx, "private:1", "行吧（这气氛有点绷不住了 我缓缓）我先去写作业"))
    assert [s.data["text"] for _c, segs in ctx.sent for s in segs] == ["行吧我先去写作业"]


def test_line_of_only_aside_is_skipped(plugin):
    ctx = FakeCtx()
    asyncio.run(plugin.send_lines(ctx, "private:1", "（嘴上这么说 心里已经美滋滋了）"))
    assert ctx.sent == []


def test_half_width_parens_also_stripped(plugin):
    ctx = FakeCtx()
    asyncio.run(plugin.send_lines(ctx, "private:1", "知道了(偷偷记小本本上)"))
    assert [s.data["text"] for _c, segs in ctx.sent for s in segs] == ["知道了"]