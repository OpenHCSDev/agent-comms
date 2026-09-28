"""Decode saved Pi entries once; native files remain the sole history authority."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from .declared_family import DeclaredFamily
from .messages import Message
from .pi_payloads import PiMessage, PiPayload
from .routing import TurnRouting
from .transcript_events import NoticeTranscript, TranscriptEvent
from .transcript_routes import InputDisplay


@dataclass(frozen=True)
class TranscriptProjection:
    routing: TurnRouting | None = None
    input_display: InputDisplay | None = None
    sent_tool_message: Callable[[str, str, bool], Message | None] | None = None


@dataclass(frozen=True, kw_only=True)
class NativeEntry(PiPayload, DeclaredFamily, affix="Entry"):
    wire_tag = "type"
    opaque: ClassVar[bool] = False
    id: str | None = None
    is_message: ClassVar[bool] = False
    assistant_message: ClassVar[bool] = False

    @classmethod
    def wire_member(cls, value):
        try:
            return cls.decode(value.get("type"))
        except ValueError:
            return UnknownEntry

    @classmethod
    def read(cls, raw: bytes) -> NativeEntry:
        return cls.from_wire(json.loads(raw))

    @property
    def model_choice(self) -> tuple[str, str] | None:
        return None

    @property
    def input_id(self) -> str | None:
        return None

    def events(self, context: TranscriptProjection) -> list[TranscriptEvent]:
        return []

    @property
    def unread_reply(self) -> bool:
        return False


@dataclass(frozen=True, kw_only=True)
class MessageEntry(NativeEntry):
    message: PiMessage
    is_message = True

    @property
    def assistant_message(self) -> bool:
        return self.message.assistant

    @property
    def input_id(self) -> str | None:
        return self.message.input_id

    def events(self, context: TranscriptProjection) -> list[TranscriptEvent]:
        return self.message.transcript_events(context)

    @property
    def unread_reply(self) -> bool:
        return self.message.unread_reply


@dataclass(frozen=True, kw_only=True)
class CompactionEntry(NativeEntry):
    summary: str = ""

    def events(self, context: TranscriptProjection) -> list[TranscriptEvent]:
        text = self.summary.strip()
        return [NoticeTranscript(f"## Context compacted\n\n{text}")] if text else []


@dataclass(frozen=True, kw_only=True)
class UnknownEntry(NativeEntry):
    payload: dict[str, Any]
    opaque = True


@dataclass(frozen=True, kw_only=True)
class ModelChangeEntry(NativeEntry):
    provider: str
    model_id: str = field(metadata={"wire_name": "modelId"})

    @property
    def model_choice(self) -> tuple[str, str] | None:
        return (self.provider, self.model_id) if self.provider and self.model_id else None
