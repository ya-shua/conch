import asyncio

from core.message import Message, Segment
from core.pipeline import Merger


def msg(text: str) -> Message:
    return Message(
        conv_id="private:1",
        kind="private",
        sender_id="1",
        sender_name="a",
        message_id=text,
        segments=[Segment("text", {"text": text})],
        ts=0,
    )


def test_burst_merges_into_one_batch():
    async def go():
        got = []

        async def on_batch(batch):
            got.append(batch)

        m = Merger(50, on_batch)
        m.feed(msg("a"))
        m.feed(msg("b"))
        m.feed(msg("c"))
        await asyncio.sleep(0.2)

        assert len(got) == 1
        assert [x.text for x in got[0]] == ["a", "b", "c"]

    asyncio.run(go())


def test_separate_conversations_not_mixed():
    async def go():
        got = []

        async def on_batch(batch):
            got.append(batch)

        m = Merger(40, on_batch)
        m.feed(msg("a"))
        other = msg("x")
        other.conv_id = "group:2"
        m.feed(other)
        await asyncio.sleep(0.2)

        assert len(got) == 2
        assert {b[0].conv_id for b in got} == {"private:1", "group:2"}

    asyncio.run(go())


def test_no_total_cap_keeps_waiting():
    """本人要求：总时长不设上限，她一直补就一直等。"""
    async def go():
        got = []

        async def on_batch(batch):
            got.append(batch)

        m = Merger(120, on_batch)          # max_wait 默认 0 = 不限
        for text in ("a", "b", "c", "d", "e"):
            m.feed(msg(text))
            await asyncio.sleep(0.05)      # 每 50ms 补一条，永远不到 120ms
        assert got == [], "还在补，就不该先开跑"
        await asyncio.sleep(0.3)
        assert len(got) == 1
        assert [x.text for x in got[0]] == ["a", "b", "c", "d", "e"]

    asyncio.run(go())


def test_cap_only_when_asked():
    async def go():
        got = []

        async def on_batch(batch):
            got.append(batch)

        m = Merger(60, on_batch, max_wait_ms=150)
        for text in ("a", "b", "c", "d", "e"):
            m.feed(msg(text))
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)
        assert got and len(got) >= 1

    asyncio.run(go())


def test_same_message_id_only_fed_once():
    async def go():
        got = []

        async def on_batch(batch):
            got.append(batch)

        m = Merger(30, on_batch)
        m.feed(msg("a"))
        m.feed(msg("a"))          # 协议端重连重复推的同一条
        await asyncio.sleep(0.2)

        assert len(got) == 1
        assert [x.text for x in got[0]] == ["a"]

    asyncio.run(go())