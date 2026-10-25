"""Decode saved Pi entries once; native files remain the sole history authority."""

from __future__ import annotations

from .native_failures import NativePiUnavailable

import json
from abc import ABC, abstractmethod
from contextlib import ExitStack, contextmanager
from collections.abc import Iterable
from itertools import repeat
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar, Literal, TextIO, cast

from .locked_store import DurableFile
from .native_pi import (
    NativeEvidencePrefixChanged,
    NativeInputUncommitted,
    PrivateEvidenceRead,
)
from .native_session_files import BusTranscriptFile

if TYPE_CHECKING:
    from .native_pi import NativeContextProof
from uuid import uuid4

from .declared_family import DeclaredFamily, UnknownFamilyMember
from .field_codec import (
    FieldCodec,
    InputId,
    MalformedRecord,
    RecordedTimestamp,
    Sha256Text,
    TimestampText,
    WireName,
    WireOmitDefault,
)
from .importing import ImportedSessionMetadata
from .native_input_record import NativeInputLocation
from .pi_payloads import Opaque, PiField, PiMessage, PiOmitted, PiPayload, ReportedText
from .pi_vocabulary import PendingStopReason, ThinkingLevel
from .recorded_time import GeneratedTimestamp
from .routing import TurnRouting
from .transcript_events import ImportedMessage, NoticeTranscript, TranscriptEvent
from .transcript_routes import InputDisplay, _SessionRoutes
from .turn_item import PiSessionId


@dataclass(frozen=True)
class TranscriptProjection:
    position: int = field(kw_only=True)
    routing: TurnRouting | None = None
    input_display: InputDisplay | None = None


@dataclass(frozen=True, slots=True)
class RootEntry(PiOmitted):
    """Pi writes a null parentId on a session's first entry."""


@dataclass(frozen=True, kw_only=True)
class RecordedEntry(PiPayload):
    """Shared source identity and projection; each journal declares its own clock."""

    id: str | None = None
    parent_id: Annotated[str | RootEntry, WireName("parentId")] = RootEntry()
    is_message: ClassVar[bool] = False
    assistant_message: ClassVar[bool] = False
    messages: ClassVar[tuple[PiMessage, ...]] = ()
    final_reply: ClassVar[bool] = False

    @property
    def child_parent(self) -> object:
        return self.require_entry_id()

    def imported_child(self, message: ImportedMessage, timestamp: datetime) -> dict[str, object]:
        return {
            "type": "message", "id": uuid4().hex[:16], "parentId": self.child_parent,
            "timestamp": timestamp.isoformat(),
            "message": message.pi_message(int(timestamp.timestamp() * 1000)),
        }

    @property
    def input_boundary(self):
        return any(message.user for message in self.messages)

    def require_entry_id(self) -> str:
        return self.source_coordinates.original_id

    def routing_in(self, routes: _SessionRoutes) -> TurnRouting | None:
        return routes.get(self.id)

    @property
    def source_coordinates(self):
        return self

    @abstractmethod
    def events(self, context: TranscriptProjection) -> list[TranscriptEvent]: ...

    def project_messages(self, context: TranscriptProjection, timestamps: Iterable[float]) -> list[TranscriptEvent]:
        return [event for message, timestamp in zip(self.messages, timestamps, strict=True)
                for event in message.transcript_events(context, timestamp)]

    @property
    def original_id(self):
        if not self.id:
            raise ValueError("Native publication requires an original entry ID")
        return str(self.id)


@dataclass(frozen=True, kw_only=True)
class NativeEntry(RecordedEntry, DeclaredFamily, affix="Entry"):
    wire_tag = "type"
    timestamp: Annotated[RecordedTimestamp, TimestampText]

    def events(self, context: TranscriptProjection) -> list[TranscriptEvent]:
        """Every part of a native record shares its original journal clock."""
        return self._events(context, self.timestamp.at.timestamp())

    def _events(self, context: TranscriptProjection, timestamp: float) -> list[TranscriptEvent]:
        return self.project_messages(context, repeat(timestamp, len(self.messages)))

    def imported_sources(self):
        return ()

    def require_failed_terminal(self, parent_id: str) -> None:
        raise ValueError("Native recovery requires an unambiguous failed terminal")

    @property
    def stop_reason(self):
        return PendingStopReason

    @classmethod
    def wire_member(cls, value):
        try:
            member = cls.member_named(value.get("type"))
        except UnknownFamilyMember:
            return UnknownEntry
        return member.wire_variant(value)

    @classmethod
    def wire_variant(cls, value):
        """Known external members can refine their original boundary shape."""
        return cls

    @classmethod
    def read(cls, raw: bytes) -> NativeEntry:
        return cls.from_wire(FieldCodec.parse(raw))

    @classmethod
    def from_evidence(cls, raw: dict) -> NativeEntry:
        """Strict tracked-input boundary, separate from tolerant display decoding."""
        return cls.from_wire(raw).preserve_evidence(raw)

    def preserve_evidence(self, raw: dict) -> NativeEntry:
        """Non-message members have no tracked message content to corroborate."""
        return self

    @classmethod
    def input_evidence(cls, raw: dict) -> tuple[NativeEntry, ...]:
        """Non-input entries do not contribute a tracked input proof."""
        return ()

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
    def input_id(self) -> InputId | None:
        return None

    @property
    def unread_reply(self) -> bool:
        return False


@dataclass
class ImportedSessionWriter(DurableFile, BusTranscriptFile):
    filename = "imported_sessions/{session_id}.jsonl"
    stream: TextIO
    parent: NativeEntry

    def append(self, message: ImportedMessage, timestamp: datetime) -> None:
        encoded = self.parent.imported_child(message, timestamp)
        self.stream.write(json.dumps(encoded, ensure_ascii=False) + "\n")
        self.parent = NativeEntry.from_wire(encoded)

    def append_recorded(self, event: TranscriptEvent) -> None:
        def append(message: ImportedMessage) -> None:
            at = GeneratedTimestamp.decode(event.timestamp).recorded_at("Copied conversation")
            self.append(message, datetime.fromtimestamp(at, UTC))
        event.import_to(append)


class NativeEvidenceState(ABC):
    """A read header and the earliest required input position."""

    @abstractmethod
    def read(self, reader: NativeEvidenceRead) -> SessionEntry: ...

    def observe_inputs(
        self, reader: NativeEvidenceRead, inputs: tuple[NativeInputLocation, ...],
    ) -> tuple[SessionEntry, tuple[NativeEntry, ...]]:
        if not inputs:
            raise MalformedRecord('Native input evidence requires a committed byte offset')
        minimum = min(item.session_entry_start for item in inputs)
        reader.header()
        reader.source.start_at(minimum)
        reader.state = InputNativeEvidence(minimum)
        return reader._observe(reverified=False)

    def reobserve(self, reader: NativeEvidenceRead) -> tuple[SessionEntry, tuple[NativeEntry, ...]]:
        return reader._observe(reverified=True)


class UnreadNativeEvidence(NativeEvidenceState):
    def read(self, reader: NativeEvidenceRead) -> SessionEntry:
        header = SessionEntry.from_wire(reader.source.consume_header())
        reader.entries = (header,)
        reader.state = HeaderNativeEvidence()
        return header


class HeaderNativeEvidence(NativeEvidenceState):
    def read(self, reader: NativeEvidenceRead) -> SessionEntry:
        reader.source.verify()
        return cast(SessionEntry, reader.entries[0])


@dataclass(frozen=True)
class InputNativeEvidence(HeaderNativeEvidence):
    minimum: int

    def observe_inputs(
        self, reader: NativeEvidenceRead, inputs: tuple[NativeInputLocation, ...],
    ) -> tuple[SessionEntry, tuple[NativeEntry, ...]]:
        if any(item.session_entry_start < self.minimum for item in inputs):
            reader._forget()
            return super().observe_inputs(reader, inputs)
        return reader._observe(reverified=False)

    def reobserve(self, reader: NativeEvidenceRead) -> tuple[SessionEntry, tuple[NativeEntry, ...]]:
        reader.header()
        reader.source.start_at(self.minimum)
        reader.state = self
        return reader._observe(reverified=True)


class NativeEvidenceRead:
    """Decoded original entries live only inside the acquired file resource.

    The source owner verifies initial bytes, pinned revisions, and each append
    before decoding it. No context proof, disposition or owner authority is
    retained here.
    """

    def __init__(self, source):
        self.source = source
        self._forget()

    def require_path(self, session_file):
        if self.source.path != session_file:
            raise NativePiUnavailable("Native evidence reader belongs to another source")

    @classmethod
    @contextmanager
    def open(cls, session_file):
        """One original descriptor and byte-verification lifetime for every read."""
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

    def _forget(self) -> None:
        self.entries: tuple[NativeEntry, ...] = ()
        self.inputs: dict[str, int] = {}
        self.state: NativeEvidenceState = UnreadNativeEvidence()

    def close(self):
        self.source.close()
        self._forget()

    def position(self, input_id: InputId) -> int:
        """Where the observed session committed this input; the one answer to whether it did."""
        try:
            return self.inputs[input_id]
        except KeyError as missing:
            raise NativeInputUncommitted(
                "The specified input was never durably committed") from missing

    def tracked(self, input_id: InputId) -> MessageEntry:
        """The observed entry that holds this tracked input."""
        return self.entries[self.position(input_id)].require_tracked_user()

    def index(self, appended: tuple[NativeEntry, ...]) -> None:
        """Add each newly decoded tracked input once, refusing a second entry for one input."""
        for position, entry in enumerate(appended, len(self.entries)):
            if user := entry.tracked_user:
                if user.input_id in self.inputs:
                    raise MalformedRecord(
                        "ambiguous tracked user input: one input ID on two entries")
                self.inputs[user.input_id] = position

    def header(self) -> SessionEntry:
        """Read and decode record one without discarding an existing input index."""
        try:
            return self.state.read(self)
        except MalformedRecord as error:
            self.close()
            raise NativePiUnavailable(f'Native Pi session evidence is invalid: {error}') from error

    def observe(self):
        """Read the full history."""
        return self._observe(reverified=False)

    def read_input(self, location):
        self.observe()
        return self.tracked(location.input_id)

    def stopped_input(self, input_id: InputId):
        """Read only this input's contiguous branch, ending at the next input.

        A later turn, a fork or a partial tool round cannot finish this input.
        This observes the native result; it grants no input or reply replay.
        """
        _header, entries = self.observe()
        try:
            start = self.position(input_id)
        except NativeInputUncommitted as uncommitted:
            return uncommitted.stop_reason
        last = entries[start]
        for entry in entries[start + 1:]:
            if entry.input_boundary:
                break
            if entry.parent_id != last.id:
                return PendingStopReason
            last = entry
        return last.stop_reason

    def _reverify(self):
        acquired = self.state
        self._forget()
        self.source.reverify()
        return acquired.reobserve(self)

    def _observe(self, *, reverified: bool) -> tuple[SessionEntry, tuple[NativeEntry, ...]]:
        try:
            header = self.header()
            appended = tuple(self.decode_rows(self.source.rows()))
            self.index(appended)
            entries = self.entries + appended
        except NativeEvidencePrefixChanged:
            if reverified:
                self.close()
                raise
            # Replacement, truncation or a changed prior hash enters the
            # source owner's named unverified state. Reopen once and decode
            # from byte zero; no stale entry or offset survives the transition.
            return self._reverify()
        except MalformedRecord as error:
            self.close()
            raise NativePiUnavailable(f"Native Pi session evidence is invalid: {error}") from error
        except BaseException:
            # Any other failure retires the reader's bytes too; only data refusal is reworded.
            self.close()
            raise
        self.entries = entries
        return header, entries

    def decode_rows(self, rows):
        for row in rows:
            yield NativeEntry.from_evidence(row)


class NativeInputEvidenceRead(NativeEvidenceRead):
    """Check every file byte, but decode only the header and submitted inputs.

    Keep the parent class's file handle, append handling and error behavior.
    Read input evidence from that file without adding a history index or store.
    """

    def observe(self, *inputs: NativeInputLocation) -> tuple[SessionEntry, tuple[NativeEntry, ...]]:
        return self.state.observe_inputs(self, inputs)

    def read_input(self, location: NativeInputLocation) -> MessageEntry:
        self.observe(location)
        return self.tracked(location.input_id)

    def decode_rows(self, rows):
        for row in rows:
            yield from NativeEntry.wire_member(row).input_evidence(row)


class NativeEvidenceScope(ExitStack):
    """One recipient-owned input-proof source across its corroboration lifetime.

    Only the current descriptor and its decoded bytes are held. Switching
    journals closes the previous reader; no proof, acknowledgement or disposition is
    retained. Every borrow still validates the original prefix on observation.
    """

    def __init__(self) -> None:
        super().__init__()
        self.readers: dict[Path, NativeInputEvidenceRead] = {}

    @contextmanager
    def borrow(self):
        """Retain this owner's reader on success; retire it on refused evidence."""
        try:
            yield self
        except BaseException:
            self.close()
            raise

    def observe_inputs(self, contexts: tuple[NativeContextProof, ...]) -> None:
        """Read each source from its first required input before checking any input."""
        for path in {context.session_file for context in contexts}:
            self.for_source(path).observe(*(
                context for context in contexts if context.session_file == path
            ))

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
    id: PiSessionId

    def routing_in(self, routes: _SessionRoutes) -> TurnRouting | None:
        """A session header is not an entry and holds no turn annotation."""
        return None

    @property
    def child_parent(self) -> object:
        return FieldCodec.encode(self.parent_id)

    version: int | None = None
    cwd: PiField[ReportedText | PiOmitted] = PiOmitted()
    parent_session: Annotated[
        PiSessionId | PiOmitted, WireName("parentSession"), WireOmitDefault()
    ] = PiOmitted()

    @classmethod
    def input_evidence(cls, raw: dict) -> tuple[NativeEntry, ...]:
        return (cls.from_evidence(raw),)

    def require_reopen(self) -> PiSessionId:
        """Saved native execution supports v3; display may decode older headers."""
        if self.version != 3:
            raise MalformedRecord("Strict native v3 session header required")
        return self.id


@dataclass(frozen=True, kw_only=True)
class MessageEntry(NativeEntry):
    def preserve_evidence(self, raw: dict) -> MessageEntry:
        message = raw["message"]
        # Opaque display roles must not hide a tracked ID from proof readers.
        if message.get("inputId") is not None and not self.message.user:
            raise MalformedRecord("Tracked native input must belong to a user")
        if self.message.user and isinstance(self.message.content, tuple):
            # Continued STARTED text matching must retain extended native fields.
            content = tuple(
                part.preserve_evidence(raw_part)
                for part, raw_part in zip(self.message.content, message["content"], strict=True)
            )
            return replace(self, message=replace(self.message, content=content))
        return self

    @classmethod
    def input_evidence(cls, raw: dict) -> tuple[NativeEntry, ...]:
        message = raw.get("message")
        if not isinstance(message, dict):
            raise MalformedRecord("Native message evidence requires an object")
        if message.get("inputId") is not None:
            # Same strict original user/content/digest decoding, including the
            # rejection of tracked IDs hidden in another message role.
            return (cls.from_evidence(raw),)
        return ()

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
    def input_id(self) -> InputId | None:
        # NativeEntry declares absence as None for every entry; this hands Pi's answer over.
        return self.message.input_id.value_or(None)

    @property
    def tracked_user(self) -> MessageEntry | None:
        for input_id in self.message.input_id.present():
            if not self.message.user or not self.message.input_digest.reported or not self.id:
                raise MalformedRecord("Native Pi session has ambiguous tracked user input")
            Sha256Text.from_text(self.message.input_digest.require())
            return self
        return None

    def require_failed_terminal(self, parent_id: str) -> None:
        if self.parent_id != parent_id:
            raise ValueError("Native recovery requires an unambiguous failed terminal")
        self.message.require_failed_terminal()

    @property
    def stop_reason(self):
        return self.message.stop_reason if self.assistant_message else PendingStopReason

    @property
    def unread_reply(self) -> bool:
        return self.message.unread_reply


@dataclass(frozen=True, kw_only=True)
class CompactionEntry(NativeEntry):
    """Pi's compaction entry: the summary that replaced the history before firstKeptEntryId."""

    summary: str = ""

    def _events(self, context: TranscriptProjection, timestamp: float) -> list[TranscriptEvent]:
        text = self.summary.strip()
        return [NoticeTranscript(f"## Context compacted\n\n{text}", position=context.position, timestamp=timestamp)] if text else []


@dataclass(frozen=True, kw_only=True)
class StoredEntry(RecordedEntry):
    """A committed Pi Durable entry owns zero or more ordinary Pi messages."""

    id: int = field()
    messages: Annotated[tuple[PiMessage, ...], WireName("model")] = ()

    def events(self, context: TranscriptProjection) -> list[TranscriptEvent]:
        """Durable entries have no clock; their individual model messages do."""
        return self.project_messages(context, (
            message.timestamp.recorded_at("Durable message") for message in self.messages
        ))


class CustomEntry(NativeEntry):
    """External custom records select their declared payload once at ingress."""

    @classmethod
    def wire_variant(cls, value):
        return ImportedMetadataEntry if value.get("customType") == "agent-comms-import" else UnknownEntry


@dataclass(frozen=True, kw_only=True)
class ImportedMetadataEntry(CustomEntry):
    custom_type: Annotated[Literal["agent-comms-import"], WireName("customType")]
    data: ImportedSessionMetadata

    def imported_sources(self):
        return self.data.historical_instructions

    def wire_discriminator(self):
        return FieldCodec.encode(CustomEntry)


@dataclass(frozen=True, kw_only=True)
class UnknownEntry(Opaque, NativeEntry):
    pass


@dataclass(frozen=True, kw_only=True)
class ModelChangeEntry(NativeEntry):
    provider: str
    model_id: Annotated[str, WireName("modelId")]

    @property
    def model_choice(self) -> tuple[str, str] | None:
        return (self.provider, self.model_id) if self.provider and self.model_id else None


@dataclass(frozen=True, kw_only=True)
class ThinkingLevelChangeEntry(NativeEntry):
    thinking_level: Annotated[type[ThinkingLevel], WireName("thinkingLevel")]
