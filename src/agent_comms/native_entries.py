"""Decode saved Pi entries once; native files remain the sole history authority."""

from __future__ import annotations

import json
import re
from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field, fields, replace
from datetime import datetime
from typing import Any, ClassVar, Literal

from .pi_vocabulary import ThinkingLevel
from .declared_family import DeclaredFamily
from .messages import Message
from .pi_payloads import PiMessage, PiPayload
from .pi_rpc import unique_fields
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
    timestamp: str | None = None
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
            if entry.message.user and isinstance(entry.message.content, tuple):
                # A digest/context reader may corroborate extended native content,
                # but continued STARTED text matching must never lose extra fields.
                content = tuple(
                    part.preserve_evidence(raw_part)
                    for part, raw_part in zip(
                        entry.message.content, message["content"], strict=True
                    )
                )
                entry = replace(entry, message=replace(entry.message, content=content))
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
        """Project one journal clock onto every display part without inventing time.

        Pi owns the external ISO8601 field. Decode it once here, before splitting
        into message/tool/routing events; ACP consumers receive Unix seconds.
        Missing or invalid external time leaves history readable but undated.
        """
        timestamp = None
        if self.timestamp is not None:
            try:
                recorded = datetime.fromisoformat(self.timestamp)
                if recorded.utcoffset() is not None:
                    timestamp = recorded.timestamp()
            except (ValueError, OverflowError):
                pass
        return [replace(event, timestamp=timestamp) for event in self._events(context)]

    def _events(self, context: TranscriptProjection) -> list[TranscriptEvent]:
        return []

    @property
    def unread_reply(self) -> bool:
        return False


@dataclass(frozen=True)
class SelectedFreshMarker(PiPayload):
    """Saved denial marker only; FreshPrivateSession retains enrollment authority."""

    strict_fields = True
    schema: Literal[1]
    thinking_level: str = field(metadata={"wire_name": "thinkingLevel", "wire_choices": ThinkingLevel.selected_names})


@dataclass(frozen=True, kw_only=True)
class SessionEntry(NativeEntry):
    version: int | None = None
    selected_fresh: SelectedFreshMarker | None = field(
        default=None, metadata={"wire_name": "agentCommsSelectedFresh"}
    )

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
        if self.parent_id != parent_id:
            raise ValueError("Native recovery requires an unambiguous failed terminal")
        self.message.require_failed_terminal()

    def _events(self, context: TranscriptProjection) -> list[TranscriptEvent]:
        return self.message.transcript_events(context)

    @property
    def unread_reply(self) -> bool:
        return self.message.unread_reply


@dataclass(frozen=True, kw_only=True)
class CompactionEntry(NativeEntry):
    summary: str = ""

    def _events(self, context: TranscriptProjection) -> list[TranscriptEvent]:
        text = self.summary.strip()
        return [NoticeTranscript(f"## Context compacted\n\n{text}")] if text else []


@dataclass(frozen=True, kw_only=True)
class UnknownEntry(NativeEntry):
    payload: dict[str, Any]
    opaque = True


@dataclass(frozen=True, kw_only=True)
class StartupMetadataEntry(NativeEntry):
    """Native metadata with strict evidence decoding for startup attestation."""

    @classmethod
    def read_startup(cls, raw: bytes) -> StartupMetadataEntry:
        value = json.loads(raw, object_pairs_hook=unique_fields)
        entry = cls.from_wire(value)
        # Display readers can project external entries; authority readers require
        # the entire declared record, with no omitted or unrepresented fields.
        names = {f.metadata.get("wire_name", f.name) for f in fields(entry) if f.init}
        if set(value) != names | {entry.wire_tag}:
            raise ValueError("Native startup metadata fields are incomplete or unexpected")
        if (
            entry.id is None
            or re.fullmatch(r"[0-9a-f]{8}", entry.id) is None
            or not entry.timestamp
            or (
                entry.parent_id is not None
                and re.fullmatch(
                    r"[0-9a-f]{8}(?:-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?",
                    entry.parent_id,
                )
                is None
            )
        ):
            raise ValueError("Native startup metadata identity is invalid")
        return entry

    @classmethod
    def wire_member(cls, value):
        # Unknown saved entries are displayable, but never startup authority.
        return cls.decode(value.get(cls.wire_tag))

    @abstractmethod
    def matches_startup(self, model: tuple[str, str], thinking_level: str) -> bool:
        """Whether this metadata records the configured startup selection."""


@dataclass(frozen=True, kw_only=True)
class ModelChangeEntry(StartupMetadataEntry):
    provider: str
    model_id: str = field(metadata={"wire_name": "modelId"})

    @property
    def model_choice(self) -> tuple[str, str] | None:
        return (self.provider, self.model_id) if self.provider and self.model_id else None

    def matches_startup(self, model: tuple[str, str], thinking_level: str) -> bool:
        return self.model_choice == model


@dataclass(frozen=True, kw_only=True)
class ThinkingLevelChangeEntry(StartupMetadataEntry):
    thinking_level: type[ThinkingLevel] = field(metadata={"wire_name": "thinkingLevel"})

    def matches_startup(self, model: tuple[str, str], thinking_level: str) -> bool:
        return self.thinking_level is ThinkingLevel.decode(thinking_level)
