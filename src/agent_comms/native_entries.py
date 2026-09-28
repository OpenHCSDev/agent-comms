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
    parent_id: str | None = field(default=None, metadata={"wire_name": "parentId"})
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

    @classmethod
    def from_evidence(cls, raw: dict) -> NativeEntry:
        """Strict tracked-input boundary, separate from tolerant display decoding."""
        entry = cls.from_wire(raw)
        if isinstance(entry, MessageEntry):
            message = raw["message"]
            # Opaque display roles must not hide a tracked ID from proof readers.
            if message.get("inputId") is not None and not entry.message.user:
                raise ValueError("Tracked native input must belong to a user")
            if entry.message.user and "content" in message:
                content = entry.message.content
                represented = (
                    [part.to_wire() for part in content]
                    if isinstance(content, tuple)
                    else content
                )
                if message["content"] != represented:
                    raise ValueError("Native user content contains unrepresented evidence fields")
        return entry

    @classmethod
    def read_evidence(cls, session_file):
        """Decode once behind the existing strict private-file trust boundary."""
        from .native_pi import NativePiUnavailable, _private_session_dir, _read_private_file

        _private_session_dir(session_file.parent)
        raw = _read_private_file(session_file)
        try:
            entries = tuple(cls.from_evidence(row) for row in raw)
            if not entries or not isinstance(entries[0], SessionEntry):
                raise ValueError("Native Pi session header is invalid")
            entries[0].require_header()
        except (ValueError, TypeError, KeyError) as error:
            raise NativePiUnavailable(f"Native Pi session evidence is invalid: {error}") from error
        return entries[0], entries

    @staticmethod
    def tracked_users(entries):
        from .native_pi import NativePiUnavailable

        tracked = {}
        try:
            for entry in entries:
                user = entry.tracked_user
                if user is None:
                    continue
                if user.input_id in tracked:
                    raise ValueError("duplicate tracked input")
                tracked[user.input_id] = user
        except (ValueError, TypeError) as error:
            raise NativePiUnavailable(
                "Native Pi session has ambiguous tracked user input"
            ) from error
        return tracked

    @property
    def tracked_user(self) -> MessageEntry | None:
        return None

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
class SessionEntry(NativeEntry):
    version: int | None = None

    def require_header(self) -> None:
        if not self.id:
            raise ValueError("Native Pi session header is invalid")


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

    @property
    def tracked_user(self) -> MessageEntry | None:
        from .native_pi import _DIGEST, _INPUT_ID

        if self.message.input_id is None:
            return None
        if (
            not self.message.user
            or not _INPUT_ID.fullmatch(self.message.input_id)
            or self.message.input_digest is None
            or not _DIGEST.fullmatch(self.message.input_digest)
            or not self.id
        ):
            raise ValueError("Native Pi session has ambiguous tracked user input")
        return self

    def require_failed_terminal(self, parent_id: str) -> None:
        if (
            self.parent_id != parent_id
            or not self.message.assistant
            or self.message.stop_reason != "error"
            or not self.message.error_message
            or self.message.content != ()
        ):
            raise ValueError("Native recovery requires an unambiguous failed terminal")

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
