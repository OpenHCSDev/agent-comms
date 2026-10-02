"""Decode saved Pi entries once; native files remain the sole history authority."""

from __future__ import annotations

import json
import re
from contextlib import ExitStack, contextmanager
from abc import abstractmethod
from dataclasses import dataclass, field, fields, replace
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Literal

from .pi_vocabulary import ThinkingLevel
from .declared_family import DeclaredFamily
from .pi_payloads import PiMessage, PiPayload
from .native_compaction_request import NativeSummaryPayload
from .pi_summary_payloads import ManagedSummaryFiles, ManagedSummaryMetadata
from .pi_rpc import unique_fields
from .routing import TurnRouting
from .transcript_events import NoticeTranscript, TranscriptEvent
from .transcript_routes import InputDisplay


@dataclass(frozen=True)
class TranscriptProjection:
    routing: TurnRouting | None = None
    input_display: InputDisplay | None = None


@dataclass(frozen=True, kw_only=True)
class NativeEntryCoordinates(PiPayload):
    """Original external ancestry fields; opaque entries project this same owner."""
    id: str | None = None
    parent_id: str | None = field(default=None, metadata={"wire_name": "parentId"})

    @property
    def original_id(self):
        if not self.id:
            raise ValueError("Native publication requires an original entry ID")
        return self.id


@dataclass(frozen=True, kw_only=True)
class NativeEntry(NativeEntryCoordinates, DeclaredFamily, affix="Entry"):
    wire_tag = "type"
    opaque: ClassVar[bool] = False
    timestamp: str | None = None
    is_message: ClassVar[bool] = False
    assistant_message: ClassVar[bool] = False
    input_boundary: ClassVar[bool] = False
    final_reply: ClassVar[bool] = False

    def retained_tool_calls(self):
        return ()

    def covered_prefix(self, evidence, branch, db):
        """This entry supplies no journaled compaction or creation coverage."""
        return frozenset()

    def retained_tool_facts(self, session, originals):
        return ()

    def require_artifact_request(self, request):
        raise ValueError("Native entry is not a completed file operation")

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

    def require_entry_id(self) -> str:
        return self.source_coordinates.original_id

    @property
    def source_coordinates(self):
        return self

    def require_tracked_user(self) -> MessageEntry:
        tracked = self.tracked_user
        if tracked is None:
            raise ValueError("Native publication requires an original tracked input")
        return tracked

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


class NativeEvidenceRead:
    """Decoded original entries live only inside the acquired file resource.

    Every observation verifies all previously read bytes before decoding the
    append. No context proof, disposition or owner authority is retained here.
    """

    def __init__(self, source):
        self.source = source
        self.entries: tuple[NativeEntry, ...] = ()

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

    def observe(self):
        from .native_pi import NativePiUnavailable, _private_session_dir

        try:
            _private_session_dir(self.source.path.parent)
            appended = tuple(self.decode_rows(self.source.rows()))
            entries = self.entries + appended
            if not entries or not isinstance(entries[0], SessionEntry):
                raise ValueError("Native Pi session header is invalid")
            entries[0].require_header()
            _private_session_dir(self.source.path.parent)
        except NativePiUnavailable:
            self.close()
            raise
        except (ValueError, TypeError, KeyError) as error:
            self.close()
            raise NativePiUnavailable(f"Native Pi session evidence is invalid: {error}") from error
        self.entries = entries
        return entries[0], entries

    def decode_rows(self, rows):
        return (NativeEntry.from_evidence(row) for row in rows)

    def retained_task_facts(self, witness):
        """Project only the witnessed branch of this original acquired resource.

        Lookup maps are confined to this captured read. They have no persistent
        storage, refresh lifecycle, publication or semantic authority.
        """
        from .native_session_reopen import NativeSessionIdentity

        witness.require_session(str(self.source.path))
        witness.require_current_file(self.source.path)
        header, entries = self.observe()
        if header.id != witness.session_id:
            raise ValueError("Native retained facts belong to another session")
        branch = self.branch(witness.leaf_id, entries)
        calls = {}
        facts = []
        session = NativeSessionIdentity(header.id, str(self.source.path))
        for entry in branch:
            for call in entry.retained_tool_calls():
                if call.id in calls:
                    raise ValueError("Native retained SDK call identity was repeated")
                calls[call.id] = entry
            facts.extend(entry.retained_tool_facts(session, calls))
        witness.require_current_file(self.source.path)
        return tuple(facts)

    def entry_index(self, entries):
        """Resolve original coordinates only inside this acquired read."""
        originals = {}
        for entry in entries:
            coordinates = entry.source_coordinates
            originals[coordinates.original_id] = (entry, coordinates.parent_id)
        if len(originals) != len(entries):
            raise ValueError("Native source has ambiguous original entry identities")
        return originals

    def branch(self, leaf_id, entries):
        """Resolve original ancestry inside this acquired source, never a catalog."""
        originals = self.entry_index(entries)
        branch = []
        identity = leaf_id
        while identity is not None:
            try:
                entry, parent = originals.pop(identity)
            except KeyError as error:
                raise ValueError("Native retained branch is missing or cyclic") from error
            branch.append(entry)
            identity = parent
        return tuple(reversed(branch))

    def recorded_source_prefix(self, entries, contexts):
        """Locate retained ancestry at corroborated live-recorded input anchors.

        The coordinator binds each anchor to this exact admitted session path;
        its native journal independently corroborates the recorded generation.
        An ancestor is retained source, not an input-delivery receipt, a fork
        creation, or a claim that every ancestor entered the model context.
        Sidecar-only context records cannot supply these anchors.
        """
        originals = self.entry_index(entries)
        covered = set()
        for context in contexts:
            if context.session_file != self.source.path:
                raise ValueError("Recorded source anchor belongs to another session file")
            pending = set()
            identity = context.session_entry_id
            while identity is not None and identity not in covered:
                try:
                    entry, identity = originals.pop(identity)
                except KeyError as error:
                    raise ValueError("Recorded source ancestry is missing or cyclic") from error
                pending.add(entry.require_entry_id())
            covered.update(pending)
        return frozenset(covered)

    def covered_prefix(self, entries, db):
        from .compaction_records import NativeForkCreation

        inherited = NativeForkCreation.recorded_prefix(db, self, entries)
        branch = self.branch(entries[-1].require_entry_id(), entries)
        entry = next((entry for entry in reversed(branch)
            if entry.require_entry_id() not in inherited and isinstance(entry, CompactionEntry)), entries[0])
        return inherited | entry.covered_prefix(self, branch, db)


class NativeInputEvidenceRead(NativeEvidenceRead):
    """Verify every source byte; decode only the header and original input records.

    The inherited descriptor, append and refusal lifetime is unchanged. This is
    an acquired input-proof projection, not a history index or another store.
    """

    def decode_rows(self, rows):
        for index, row in enumerate(rows):
            # The first physical record must remain the original session header.
            entry = (NativeEntry.from_evidence(row) if index == 0 and not self.entries
                     else NativeEntry.wire_member(row).input_evidence(row))
            if entry is not None:
                yield entry


class NativeEvidenceScope(ExitStack):
    """One acquired input-proof source for a bounded corroboration operation.

    Only the current descriptor and its decoded bytes are held. Switching
    journals closes the previous reader; no proof, receipt or disposition is
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


@dataclass(frozen=True)
class SelectedFreshMarker(PiPayload):
    """Saved denial marker only; FreshPrivateSession retains enrollment authority."""

    strict_fields = True
    schema: Literal[1]
    thinking_level: str = field(
        metadata={"wire_name": "thinkingLevel", "wire_choices": ThinkingLevel.selected_names}
    )


@dataclass(frozen=True, kw_only=True)
class SessionEntry(NativeEntry):
    version: int | None = None
    cwd: str | None = field(default=None, metadata={"wire_omit_default": True})
    parent_session: str | None = field(default=None, metadata={
        "wire_name": "parentSession", "wire_omit_default": True,
    })
    selected_fresh: SelectedFreshMarker | None = field(
        default=None, metadata={"wire_name": "agentCommsSelectedFresh"}
    )

    @classmethod
    def input_evidence(cls, raw: dict) -> NativeEntry:
        return cls.from_evidence(raw)

    def require_header(self) -> None:
        if not self.id:
            raise ValueError("Native Pi session header is invalid")


@dataclass(frozen=True, kw_only=True)
class MessageEntry(NativeEntry):
    @classmethod
    def input_evidence(cls, raw: dict) -> NativeEntry | None:
        message = raw["message"]
        if not isinstance(message, dict):
            raise ValueError("Native message evidence requires an object")
        if message.get("inputId") is not None:
            # Same strict original user/content/digest decoding, including the
            # rejection of tracked IDs hidden in another message role.
            return cls.from_evidence(raw)
        return None

    message: PiMessage
    is_message = True

    def retained_tool_calls(self):
        return self.message.retained_tool_calls()

    def retained_tool_facts(self, session, originals):
        return self.message.retained_tool_facts(session, self, originals)

    def require_artifact_request(self, request):
        return self.message.require_artifact_request(request)

    @property
    def input_boundary(self):
        return self.message.user

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
        from .native_pi import _DIGEST
        from .native_input_record import NativeInputIdText

        if self.message.input_id is None:
            return None
        NativeInputIdText.decode(self.message.input_id)
        if (
            not self.message.user
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
    """External native summaries grant no managed journal-cut coverage."""

    summary: str = ""

    @classmethod
    def wire_variant(cls, value):
        # This is the original Pi ingress, before FieldCodec constructs members.
        details = value.get("details")
        if isinstance(details, dict) and "agentCommsCommit" in details:
            return ManagedCompactionEntry
        return cls

    def _events(self, context: TranscriptProjection) -> list[TranscriptEvent]:
        text = self.summary.strip()
        return [NoticeTranscript(f"## Context compacted\n\n{text}")] if text else []


@dataclass(frozen=True, kw_only=True)
class ManagedCompactionEntry(CompactionEntry, NativeSummaryPayload):
    """The native payload owns validation/digests; the journal owns the cut."""

    summary: str
    first_kept_entry_id: str = field(metadata={"wire_name": "firstKeptEntryId"})
    details: ManagedSummaryFiles | ManagedSummaryMetadata

    def to_wire(self):
        value = super().to_wire()
        value["type"] = CompactionEntry.declared_name
        return value

    def covered_prefix(self, evidence, branch, db):
        from .compaction_records import CompactionOperation

        operation = CompactionOperation.one(db, commit_id=self.details.agent_comms_commit.commit_id)
        if operation is None:
            # A copied marker is not an original journal operation.
            return super().covered_prefix(evidence, branch, db)
        return operation.covered_prefix(self, evidence, branch)


@dataclass(frozen=True, kw_only=True)
class UnknownEntry(NativeEntry):
    payload: dict[str, Any]
    opaque = True

    @property
    def source_coordinates(self):
        # Decode once for this source read. No parallel current identity fields
        # are populated on an opaque extension record.
        return NativeEntryCoordinates.from_wire(self.payload)


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
