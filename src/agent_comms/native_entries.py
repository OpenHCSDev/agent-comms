"""Decode saved Pi entries once; native files remain the sole history authority."""

from __future__ import annotations

import json
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Literal

from .pi_vocabulary import ThinkingLevel
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec, MalformedRecord
from .pi_payloads import PiMessage, PiPayload
from .routing import TurnRouting
from .transcript_events import NoticeTranscript, TranscriptEvent
from .transcript_routes import InputDisplay
from .importing import ImportedSessionMetadata


@dataclass(frozen=True)
class TranscriptProjection:
    routing: TurnRouting | None = None
    input_display: InputDisplay | None = None


@dataclass(frozen=True, kw_only=True)
class RecordedEntry(PiPayload):
    """Original external ancestry fields; opaque entries project this same owner."""
    id: str | None = None
    parent_id: str | None = field(default=None, metadata={"wire_name": "parentId"})
    timestamp: str | None = None
    is_message: ClassVar[bool] = False
    assistant_message: ClassVar[bool] = False
    messages: ClassVar[tuple[PiMessage, ...]] = ()
    final_reply: ClassVar[bool] = False

    @property
    def input_boundary(self):
        return any(message.user for message in self.messages)

    def require_entry_id(self) -> str:
        return self.source_coordinates.original_id

    @property
    def source_coordinates(self):
        return self

    @property
    def recorded_at(self) -> datetime | None:
        """Pi's external ISO8601 time, decoded once for tolerant transcript display."""
        if self.timestamp is None:
            return None
        try:
            recorded = datetime.fromisoformat(self.timestamp)
            recorded.timestamp()
        except (ValueError, OverflowError):
            return None
        return recorded if recorded.utcoffset() is not None else None

    def events(self, context: TranscriptProjection) -> list[TranscriptEvent]:
        """Project one journal clock onto every display part without inventing time.

        Decode it once, before splitting into message/tool/routing events; ACP
        consumers receive Unix seconds.
        """
        recorded = self.recorded_at
        timestamp = recorded.timestamp() if recorded is not None else None
        return [replace(event, timestamp=timestamp) for event in self._events(context)]

    def _events(self, context: TranscriptProjection) -> list[TranscriptEvent]:
        return [event for message in self.messages for event in message.transcript_events(context)]

    @property
    def original_id(self):
        if not self.id:
            raise ValueError("Native publication requires an original entry ID")
        return str(self.id)


@dataclass(frozen=True, kw_only=True)
class NativeEntry(RecordedEntry, DeclaredFamily, affix="Entry"):
    wire_tag = "type"
    opaque: ClassVar[bool] = False

    def imported_sources(self):
        return ()

    @classmethod
    def wire_member(cls, value):
        try:
            member = cls.decode(value.get("type"))
        except ValueError:
            return UnknownEntry
        return member.wire_variant(value)

    @classmethod
    def wire_variant(cls, value):
        """Known external members can refine their original boundary shape."""
        return cls

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
                raise MalformedRecord("Tracked native input must belong to a user")
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
    def input_evidence(cls, raw: dict) -> NativeEntry | None:
        """Non-input entries do not contribute a tracked input proof."""
        return None

    @classmethod
    def read_evidence(cls, session_file):
        """Decode once behind the existing strict private-file trust boundary."""
        with cls.open_evidence(session_file) as evidence:
            return evidence.observe()

    @classmethod
    @contextmanager
    def open_evidence(cls, session_file):
        """Acquire a source reader, never input acceptance or replay authority."""
        with NativeEvidenceRead.open(session_file) as evidence:
            yield evidence

    @classmethod
    @contextmanager
    def open_input_evidence(cls, session_file):
        with NativeInputEvidenceRead.open(session_file) as evidence:
            yield evidence

    @property
    def tracked_user(self) -> MessageEntry | None:
        return None

    def require_tracked_user(self) -> MessageEntry:
        tracked = self.tracked_user
        if tracked is None:
            raise MalformedRecord("Native publication requires an original tracked input")
        return tracked

    @property
    def model_choice(self) -> tuple[str, str] | None:
        return None

    @property
    def input_id(self) -> str | None:
        return None

    @property
    def unread_reply(self) -> bool:
        return False


class NativeEvidenceRead:
    """Decoded original entries live only inside the acquired file resource.

    The source owner verifies initial bytes, pinned revisions, and each append
    before decoding it. No context proof, disposition or owner authority is
    retained here.
    """

    def __init__(self, source):
        self.source = source
        self.entries: tuple[NativeEntry, ...] = ()
        # Each tracked input's position in entries, indexed once as its entry is decoded.
        self.inputs: dict[str, int] = {}

    def require_path(self, session_file):
        from .native_pi import NativePiUnavailable

        if self.source.path != session_file:
            raise NativePiUnavailable("Native evidence reader belongs to another source")

    @classmethod
    @contextmanager
    def open(cls, session_file):
        """One original descriptor and byte-verification lifetime for every read."""
        from .native_pi import PrivateEvidenceRead, _private_session_dir

        _private_session_dir(session_file.parent)
        with PrivateEvidenceRead.open(session_file) as source:
            evidence = cls(source)
            try:
                yield evidence
            finally:
                evidence.close()

    @classmethod
    @contextmanager
    def borrow(cls, session_file: Path, reader: NativeEvidenceRead | None = None):
        """Own acquisition and refusal cleanup, without borrowing proof authority.

        A supplied reader stays acquired by its original scope after success.
        Failed corroboration or cancellation retires its bytes and descriptor;
        a consumer cannot continue using an observation after that refusal.
        """
        session_file = Path(session_file).absolute()
        if reader is None:
            with cls.open(session_file) as acquired:
                yield acquired
        else:
            try:
                reader.require_path(session_file)
                yield reader
            except BaseException:
                reader.close()
                raise

    def close(self):
        self.source.close()
        self.entries = ()
        self.inputs = {}

    def tracked(self, input_id: str) -> MessageEntry:
        """The observed entry that holds this tracked input."""
        return self.entries[self.inputs[input_id]].require_tracked_user()

    def index(self, appended: tuple[NativeEntry, ...]) -> None:
        """Add each newly decoded tracked input once, refusing a second entry for one input."""
        for position, entry in enumerate(appended, len(self.entries)):
            if user := entry.tracked_user:
                if user.input_id in self.inputs:
                    raise MalformedRecord("ambiguous tracked user input: one input ID on two entries")
                self.inputs[user.input_id] = position

    def observe(self):
        """Decode the captured cut."""
        return self._observe(reverified=False)

    def stopped_input(self, input_id: str):
        """Read only this input's contiguous branch, ending at the next input.

        A later turn, a fork or a partial tool round cannot finish this input.
        This observes the native result; it grants no input or reply replay.
        """
        from .pi_vocabulary import PendingStopReason

        _header, entries = self.observe()
        if input_id not in self.inputs:
            return PendingStopReason
        last = self.tracked(input_id)
        for entry in entries[self.inputs[input_id] + 1:]:
            if entry.input_boundary:
                break
            if entry.parent_id != last.id:
                return PendingStopReason
            last = entry
        if isinstance(last, MessageEntry) and last.assistant_message:
            return last.message.stop_reason
        return PendingStopReason

    def _reverify(self):
        self.entries = ()
        self.inputs = {}
        self.source.reverify_from_start()
        return self._observe(reverified=True)

    def _observe(self, *, reverified: bool):
        from .native_pi import (
            NativeEvidencePrefixChanged,
            NativePiUnavailable,
            _private_session_dir,
        )

        try:
            _private_session_dir(self.source.path.parent)
            appended = tuple(self.decode_rows(self.source.rows()))
            self.index(appended)
            entries = self.entries + appended
            if not entries or not isinstance(entries[0], SessionEntry):
                raise MalformedRecord("Native Pi session header is invalid")
            entries[0].require_header()
            _private_session_dir(self.source.path.parent)
        except NativeEvidencePrefixChanged:
            if reverified:
                self.close()
                raise
            # Replacement, truncation or a changed prior hash enters the
            # source owner's named unverified state. Reopen once and decode
            # from byte zero; no stale entry or offset survives the transition.
            return self._reverify()
        except NativePiUnavailable:
            self.close()
            raise
        except MalformedRecord as error:
            self.close()
            raise NativePiUnavailable(f"Native Pi session evidence is invalid: {error}") from error
        self.entries = entries
        return entries[0], entries

    def decode_rows(self, rows):
        for row in rows:
            yield NativeEntry.from_evidence(row)


class NativeInputEvidenceRead(NativeEvidenceRead):
    """Verify every source byte; decode only the header and original input records.

    The inherited descriptor, append and refusal lifetime is unchanged. This is
    an acquired input-proof projection, not a history index or another store.
    """

    def decode_rows(self, rows):
        for index, row in enumerate(rows):
            # The first physical record must remain the original session header.
            if index == 0 and not self.entries:
                yield NativeEntry.from_evidence(row)
                continue
            entry = NativeEntry.wire_member(row).input_evidence(row)
            if entry is not None:
                yield entry


class NativeEvidenceScope(ExitStack):
    """One acquired input-proof source for a bounded corroboration operation.

    Only the current descriptor and its decoded bytes are held. Switching
    journals closes the previous reader; no proof, acknowledgement or disposition is
    retained. Every borrow still validates the original prefix on observation.
    """

    def __init__(self) -> None:
        super().__init__()
        self.readers: dict[Path, NativeInputEvidenceRead] = {}

    @classmethod
    @contextmanager
    def borrow(cls, scope: NativeEvidenceScope | None = None):
        """One owner chooses borrowed or newly acquired resource lifetime."""
        if scope is None:
            with cls() as acquired:
                yield acquired
        else:
            try:
                yield scope
            except BaseException:
                scope.close()
                raise

    def for_source(self, path: Path) -> NativeInputEvidenceRead:
        path = Path(path).absolute()
        if path not in self.readers:
            self.close()
            self.readers[path] = self.enter_context(NativeInputEvidenceRead.open(path))
        return self.readers[path]

    def close(self) -> None:
        try:
            super().close()
        finally:
            self.readers.clear()

    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        finally:
            self.readers.clear()


@dataclass(frozen=True, kw_only=True)
class SessionEntry(NativeEntry):
    version: int | None = None
    cwd: str | None = field(default=None, metadata={"wire_omit_default": True})
    parent_session: str | None = field(default=None, metadata={
        "wire_name": "parentSession", "wire_omit_default": True,
    })

    @classmethod
    def input_evidence(cls, raw: dict) -> NativeEntry:
        return cls.from_evidence(raw)

    def require_header(self) -> None:
        if not self.id:
            raise MalformedRecord("Native Pi session header is invalid")


@dataclass(frozen=True, kw_only=True)
class MessageEntry(NativeEntry):
    @classmethod
    def input_evidence(cls, raw: dict) -> NativeEntry | None:
        message = raw.get("message")
        if not isinstance(message, dict):
            raise MalformedRecord("Native message evidence requires an object")
        if message.get("inputId") is not None:
            # Same strict original user/content/digest decoding, including the
            # rejection of tracked IDs hidden in another message role.
            return cls.from_evidence(raw)
        return None

    message: PiMessage
    is_message = True

    @property
    def messages(self):
        return (self.message,)

    @property
    def final_reply(self):
        return self.message.final_reply

    @property
    def assistant_message(self) -> bool:
        return self.message.assistant

    @property
    def input_id(self) -> str | None:
        return self.message.input_id

    @property
    def tracked_user(self) -> MessageEntry | None:
        from .field_codec import Id128Text, Sha256Text

        if self.message.input_id is None:
            return None
        Id128Text.decode(self.message.input_id)
        if not self.message.user or self.message.input_digest is None or not self.id:
            raise MalformedRecord("Native Pi session has ambiguous tracked user input")
        Sha256Text.from_text(self.message.input_digest)
        return self

    def require_failed_terminal(self, parent_id: str) -> None:
        if self.parent_id != parent_id:
            raise ValueError("Native recovery requires an unambiguous failed terminal")
        self.message.require_failed_terminal()

    @property
    def unread_reply(self) -> bool:
        return self.message.unread_reply


@dataclass(frozen=True, kw_only=True)
class CompactionEntry(NativeEntry):
    """Pi's compaction entry: the summary that replaced the history before firstKeptEntryId."""

    summary: str = ""

    def _events(self, context: TranscriptProjection) -> list[TranscriptEvent]:
        text = self.summary.strip()
        return [NoticeTranscript(f"## Context compacted\n\n{text}")] if text else []


@dataclass(frozen=True, kw_only=True)
class StoredEntry(RecordedEntry):
    """A committed Pi Durable entry owns zero or more ordinary Pi messages."""
    id: int = field()
    messages: tuple[PiMessage, ...] = field(default=(), metadata={'wire_name': 'model'})


class CustomEntry(NativeEntry):
    """External custom records select their declared payload once at ingress."""

    @classmethod
    def wire_variant(cls, value):
        return ImportedMetadataEntry if value.get("customType") == "agent-comms-import" else UnknownEntry


@dataclass(frozen=True, kw_only=True)
class ImportedMetadataEntry(CustomEntry):
    custom_type: Literal["agent-comms-import"] = field(metadata={"wire_name": "customType"})
    data: ImportedSessionMetadata

    def imported_sources(self):
        return self.data.historical_instructions

    def wire_discriminator(self):
        return FieldCodec.encode(CustomEntry)


@dataclass(frozen=True, kw_only=True)
class UnknownEntry(NativeEntry):
    payload: dict[str, Any]
    opaque = True

    @property
    def source_coordinates(self):
        # Decode once for this source read. No parallel current identity fields
        # are populated on an opaque extension record.
        return RecordedEntry.from_wire(self.payload)


@dataclass(frozen=True, kw_only=True)
class ModelChangeEntry(NativeEntry):
    provider: str
    model_id: str = field(metadata={"wire_name": "modelId"})

    @property
    def model_choice(self) -> tuple[str, str] | None:
        return (self.provider, self.model_id) if self.provider and self.model_id else None


@dataclass(frozen=True, kw_only=True)
class ThinkingLevelChangeEntry(NativeEntry):
    thinking_level: type[ThinkingLevel] = field(metadata={"wire_name": "thinkingLevel"})
