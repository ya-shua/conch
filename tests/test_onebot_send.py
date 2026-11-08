"""发送的兜底：协议端不认系统表情时，要能去掉表情把字发出去。"""

from __future__ import annotations

import asyncio, copy
from types import SimpleNamespace

from core.onebot import OneBotClient, Segment


class FakeResp:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, bodies: list[str]) -> None:
        self.bodies = list(bodies)
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(copy.deepcopy(json))
        body = self.bodies.pop(0) if self.bodies else '{"status":"ok"}'
        return FakeResp(200, body)


def make_client(bodies: list[str]) -> tuple[OneBotClient, FakeSession]:
    cfg = SimpleNamespace(
        onebot=SimpleNamespace(http_url="http://127.0.0.1:3001", access_token=""),
        stickers=SimpleNamespace(),
    )
    client = OneBotClient(cfg, on_message=lambda m: asyncio.sleep(0))
    sess = FakeSession(bodies)
    client._session = sess  # 直接塞个假会话，不走真的网络
    return client, sess


def test_face_rejected_then_retried_without_face():
    bad = (
        '{"status":"failed","retcode":100,"data":null,'
        '"wording":"OIDB error 11000 on 0x9155_1  # privacy-ok: get emoji err:'
        'type:business, code:11000, msg:id 45  # privacy-ok sysemojis cfg not found"}'
    )
    client, sess = make_client([bad, '{"status":"ok","retcode":0,"data":{}}'])

    asyncio.run(client.send("private:123", [Segment("face", {"id": "45"}), Segment("text", {"text": "在"})]))

    assert len(sess.calls) == 2, "应该重发一次"
    assert [s["type"] for s in sess.calls[1]["message"]] == ["text"], "重发时不该带表情"


def test_other_errors_are_not_retried():
    client, sess = make_client(['{"status":"failed","wording":"群不存在"}'])
    try:
        asyncio.run(client.send("group:999", [Segment("text", {"text": "hi"})]))
    except RuntimeError as exc:
        assert "群不存在" in str(exc)
    else:
        raise AssertionError("不该吞掉别的错误")
    assert len(sess.calls) == 1