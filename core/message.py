"""协议无关的消息模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Segment:
    type: str  # text / at / reply / image / face / record / forward
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Message:
    conv_id: str  # private:123 / group:456
    kind: str  # private / group
    sender_id: str
    sender_name: str
    message_id: str
    segments: list[Segment]
    ts: int
    at_me: bool = False
    reply_to: "Message | None" = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        """所有文本段拼起来，给模型和日志用。"""
        parts = [s.data.get("text", "") for s in self.segments if s.type == "text"]
        return "".join(parts).strip()

    def brief(self, limit: int = 200) -> str:
        text = self.text.replace("\n", " ")
        return text[:limit] + ("…" if len(text) > limit else "")


def make_text(text: str) -> list[Segment]:
    return [Segment("text", {"text": text})]