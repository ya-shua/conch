import asyncio

from qqbridge.core.message import Message, Segment
from qqbridge.core.pipeline import Merger


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