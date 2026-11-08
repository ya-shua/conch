"""OneBot v11 客户端：正向 WebSocket 收事件，HTTP 发动作。

M1 只需要：连得上、收得到、发得出、断线能自己回来。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

import aiohttp

from . import log
from .config import Config, norm_id
from .message import Message, Segment

log_ = log.get("onebot")

CQ_ESCAPES = (("[CQ:", "[CQ："),)


def escape_cq(text: str) -> str:
    for old, new in CQ_ESCAPES:
        text = text.replace(old, new)
    return text


def parse_segments(raw_segments: list[dict[str, Any]]) -> list[Segment]:
    out: list[Segment] = []
    for item in raw_segments or []:
        if not isinstance(item, dict):
            continue
        typ = str(item.get("type") or "")
        data = item.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        out.append(Segment(typ, data))
    return out


def parse_event(raw: dict[str, Any], self_id: str = "") -> Message | None:
    """把 OneBot 事件转成内部 Message；不是消息事件就返回 None。"""
    if raw.get("post_type") != "message":
        return None

    kind_raw = raw.get("message_type")
    if kind_raw not in ("private", "group"):
        return None
    kind = str(kind_raw)

    sender = raw.get("sender") or {}
    sender_id = norm_id(raw.get("user_id") or sender.get("user_id"))
    sender_name = str(sender.get("card") or sender.get("nickname") or sender_id)

    if kind == "group":
        conv_id = f"group:{norm_id(raw.get('group_id'))}"
    else:
        conv_id = f"private:{sender_id}"

    segments = parse_segments(raw.get("message") if isinstance(raw.get("message"), list) else [])

    at_me = False
    if self_id:
        for seg in segments:
            if seg.type == "at" and norm_id(seg.data.get("qq")) == self_id:
                at_me = True
                break

    reply_to = None
    for seg in segments:
        if seg.type == "reply":
            reply_to = Message(
                conv_id=conv_id,
                kind=kind,
                sender_id="",
                sender_name="",
                message_id=norm_id(seg.data.get("id")),
                segments=[],
                ts=0,
            )
            break

    return Message(
        conv_id=conv_id,
        kind=kind,
        sender_id=sender_id,
        sender_name=sender_name,
        message_id=norm_id(raw.get("message_id")),
        segments=segments,
        ts=int(raw.get("time") or 0) * 1000,
        at_me=at_me,
        reply_to=reply_to,
        raw=raw,
    )


class OneBotClient:
    def __init__(self, cfg: Config, on_message: Callable[[Message], Awaitable[None]]):
        self.cfg = cfg
        self._on_message = on_message
        self.self_id = ""
        self.connected = False
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None
        self._stop = False

    async def start(self) -> None:
        self._session = aiohttp.ClientSession()
        self._task = asyncio.create_task(self._run(), name="onebot-ws")

    async def stop(self) -> None:
        self._stop = True
        if self._task:
            self._task.cancel()
        if self._session:
            await self._session.close()

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop:
            try:
                await self._connect_once()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                log_.warning("连接断开：%s（%.0fs 后重试）", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10.0)

    async def _connect_once(self) -> None:
        assert self._session is not None
        headers = {}
        # SnowLuma 的 HTTP 与 WS 各有一个 token，用错就是 401
        ws_token = getattr(self.cfg.onebot, "ws_access_token", "") or self.cfg.onebot.access_token
        if ws_token:
            headers["Authorization"] = f"Bearer {ws_token}"

        async with self._session.ws_connect(self.cfg.onebot.ws_url, headers=headers, heartbeat=30) as ws:
            self.connected = True
            log_.info("已连上 %s", self.cfg.onebot.ws_url)
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_payload(json.loads(msg.data))
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        self.connected = False

    async def _handle_payload(self, payload: dict[str, Any]) -> None:
        if payload.get("post_type") == "meta_event" and payload.get("meta_event_type") == "lifecycle":
            self.self_id = norm_id(payload.get("self_id"))
            return
        sid = norm_id(payload.get("self_id"))
        if sid:
            self.self_id = sid
        message = parse_event(payload, self.self_id)
        if message is not None:
            await self._on_message(message)

    async def call(self, action: str, params: dict[str, Any] | None = None) -> Any:
        """通用动作调用，用于 get_group_list 这类只读接口。"""
        url = f"{self.cfg.onebot.http_url}/{action}"
        headers = {}
        if self.cfg.onebot.access_token:
            headers["Authorization"] = f"Bearer {self.cfg.onebot.access_token}"
        assert self._session is not None
        async with self._session.post(url, json=params or {}, headers=headers, timeout=20) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"{action} 失败 HTTP {resp.status}: {body[:200]}")
            data = json.loads(body) if body else {}
            if data.get("status") not in (None, "ok"):
                raise RuntimeError(f"{action} 失败: {body[:200]}")
            return data.get("data")

    async def send(self, conv_id: str, segments: list[Segment]) -> None:
        if ":" not in conv_id:
            raise ValueError(f"非法会话 id：{conv_id}")
        kind, target = conv_id.split(":", 1)
        if kind not in ("private", "group") or not target.isdigit():
            raise ValueError(f"非法会话 id：{conv_id}")

        action = "send_group_msg" if kind == "group" else "send_private_msg"
        params_key = "group_id" if kind == "group" else "user_id"

        built = []
        for seg in segments:
            if seg.type == "text":
                built.append({"type": "text", "data": {"text": escape_cq(str(seg.data.get("text", "")))}})
            elif seg.type == "at":
                qq = norm_id(seg.data.get("qq"))
                if qq == "all":
                    continue  # 禁止 @全体
                built.append({"type": "at", "data": {"qq": qq}})
            else:
                built.append({"type": seg.type, "data": seg.data})

        url = f"{self.cfg.onebot.http_url}/{action}"
        payload = {params_key: int(target) if target.isdigit() else target, "message": built}
        headers = {}
        if self.cfg.onebot.access_token:
            headers["Authorization"] = f"Bearer {self.cfg.onebot.access_token}"

        try:
            await self._post_message(url, payload, headers)
            return
        except RuntimeError as exc:
            # 协议端没配系统表情表时，消息里带 [CQ:face] 会被整条拒掉：
            #   OIDB error 11000 ... sysemojis cfg not found  # privacy-ok
            # 把表情段摘掉重发一次 —— 少个表情，总比整句发不出去强。
            text = str(exc).lower()
            if "emoji" not in text and "11000" not in text:  # privacy-ok
                raise
            stripped = [seg for seg in built if seg.get("type") != "face"]
            if len(stripped) == len(built):
                raise
            payload["message"] = stripped
            log_.warning("表情发不出去，去掉表情重发（%s）", conv_id)
            await self._post_message(url, payload, headers)

    async def _post_message(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> None:
        assert self._session is not None
        async with self._session.post(url, json=payload, headers=headers, timeout=20) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"发送失败 HTTP {resp.status}: {body[:200]}")
            data = json.loads(body) if body else {}
            if data.get("status") not in (None, "ok"):
                raise RuntimeError(f"发送失败: {body[:200]}")
