"""Bounded native file traversal; decode once per visited record, no extra index."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Generator, Sequence
from contextlib import closing, contextmanager
from itertools import takewhile
from math import inf
from pathlib import Path

from agent_comms.coordination.errors import TranscriptChanged
from agent_comms.errors import RelationViolationError
from agent_comms.execution.native_entries import (
    MessageEntry,
    NativeEntry,
    RootEntry,
    SessionEntry,
)
from agent_comms.execution.native_failures import NativePiUnavailable
from agent_comms.execution.native_input_proof import (
    NativeContextJournal,
    NativeContextProof,
    NativeInputLocation,
)
from agent_comms.execution.native_output import NativeRecordLocation
from agent_comms.execution.native_record_file import (
    MissingSessionCut,
    PresentSessionCut,
    ReadsNativeInputs,
    SessionCut,
    record_end,
    reverse_records,
)
from agent_comms.execution.native_records import NativeRecord, NoNativeInput
from agent_comms.execution.native_session_files import NativeSessionFiles
from agent_comms.execution.native_session_reopen import NativeSessionIdentity
from agent_comms.execution.transcript_sources import TranscriptSource
from agent_comms.execution.turn_item import FreshNativeId, NativeItemId
from agent_comms.nominal.field_codec import MalformedRecord
from agent_comms.registry.thread_identity import RecordedThreadIncarnation
from agent_comms.storage.private_path import FileRevision
from agent_comms.storage.recorded_time import EventTime, RecordedEventTime, UnrecordedEventTime
from agent_comms.team.rule_deal import Speaking, Voice



class NativeTranscript(ReadsNativeInputs, TranscriptSource):
    def __init__(self, path: Path):
        self.path = path

    def completed_input(self, context: NativeContextProof) -> bool:
        """Start at the proved input byte offset; never borrow the following input's final."""
        cut = self.cut()
        with closing(self.forward(context.session_entry_start, cut.offset)) as records:
            anchor = next(records)
            if not anchor.project(lambda original: original.entry.require_entry_id() == context.session_entry_id):
                raise TranscriptChanged("Native answer input changed after its proof was read")
            own_turn = takewhile(
                lambda record: not record.project(lambda original: original.entry.input_boundary), records)
            finals = tuple(record for record in own_turn if record.project(lambda original:
                original.entry.final_reply and any(
                    text for event in original.events for text in event.assistant_parts())))
            answered = anchor in self.inputs_for(finals, floor=context.session_entry_start)
        if not cut.holds(self.cut(), cut.offset):
            raise TranscriptChanged("Native answer source changed during its read")
        return answered

    def _tool_call_times(self, since: float = -inf) -> Generator[float]:
        try:
            with closing(self.tail()) as entries:
                for entry in entries:
                    timestamp = entry.timestamp.at.timestamp()
                    if timestamp < since:
                        return
                    if any(message.assistant and message.tool_calls
                           for message in entry.messages):
                        yield timestamp
        except FileNotFoundError:
            return

    def last_tool_call(self) -> EventTime:
        with closing(self._tool_call_times()) as times:
            return next(
                (RecordedEventTime(timestamp) for timestamp in times), UnrecordedEventTime(),
            )

    @contextmanager
    def reading(self):
        """Keep the native record owner bound across one page projection."""
        yield self

    def imported_sources(self):
        """Read original metadata before conversation, including native configuration.

        The importer writes its metadata after the header. A native saved-file
        producer may also prepend model/settings records. Stop at conversation;
        never scan the remaining history or claim this was a model request.
        """
        revision = FileRevision.from_stat(self.path.stat())
        with self.path.open("rb") as stream:
            SessionEntry.read(stream.readline())
            sources = []
            for raw in stream:
                entry = NativeEntry.read(raw)
                if entry.is_message:
                    break
                sources.extend(entry.imported_sources())
        if FileRevision.from_stat(self.path.stat()) != revision:
            raise ValueError("Imported context saved source changed while reading")
        return tuple(sources)

    def cut(self) -> SessionCut:
        try:
            stream = self.path.open("rb")
        except FileNotFoundError:
            return MissingSessionCut()
        # One open file supplies both the revision and its last record boundary.
        with stream:
            revision = FileRevision.from_file(stream)
            return PresentSessionCut(revision, record_end(stream, revision.size))

    def committed_output_cut(self, location: NativeRecordLocation) -> SessionCut:
        """Corroborate one indexed output against its original header and row."""
        if self.path.absolute() != location.path:
            raise TranscriptChanged()
        with self.path.open("rb") as stream:
            revision = FileRevision.from_file(stream)
            header = SessionEntry.read(stream.readline())
            if header.id != location.session_id or revision.size < location.session_entry_end:
                raise TranscriptChanged()
            stream.seek(location.session_entry_start - 1)
            if stream.read(1) != b"\n":
                raise TranscriptChanged()
            raw = stream.read(location.session_entry_end - location.session_entry_start)
            if (len(raw) != location.session_entry_end - location.session_entry_start
                    or not raw.endswith(b"\n")):
                raise TranscriptChanged()
            location.require_entry(NativeEntry.read(raw))
            return PresentSessionCut(revision, location.session_entry_end)

    def publication_revision(self, root: Path, recipient: RecordedThreadIncarnation):
        return self.cut().publication_revision(root, self, recipient)

    @contextmanager
    def projection(self, sources, routes):
        """Project records through their native input and the session's routes."""
        with routes.for_session(str(self.path)) as session_routes:
            def project(records, *, max_records, max_bytes):
                return sources.native_records(
                    self.fragments(records, max_records=max_records, max_bytes=max_bytes),
                    routes=session_routes, reader=self)

            yield project

    def fragments(self, records, *, max_records, max_bytes):
        """Borrow original decoded records within the caller's page budget.

        An oversized original stays whole. No cursor advances during capture;
        the traversal still owns consumption of each independent source.
        """
        fragment, used = [], 0
        for record in records:
            if fragment and used + record.size > max_bytes:
                yield tuple(fragment)
                fragment, used = [], 0
            fragment.append(record)
            used += record.size
            if len(fragment) >= max_records or used >= max_bytes:
                yield tuple(fragment)
                fragment, used = [], 0
        if fragment:
            yield tuple(fragment)

    def recorded_contexts(self, contexts, entry_ids):
        return contexts.entries(NativeSessionIdentity(self.session_id, str(self.path)),
                               tuple(NativeItemId(identity) for identity in entry_ids))

    @property
    def session_id(self):
        with self.path.open("rb") as stream:
            try:
                entry = SessionEntry.read(stream.readline())
            except MalformedRecord:
                return None
        return entry.id

    def annotation_ids(self, after: int, through: int) -> tuple[FreshNativeId, ...]:
        return tuple(identity for record in self.forward(after, through)
                     for identity in record.annotation_id.present())

    def require_publication_context(
        self,
        user: MessageEntry,
        *,
        session_entry_start: int,
        files: NativeSessionFiles,
    ) -> NativeContextProof:
        """Corroborate the existing native producer's context, without replay.

        Native inputDigest identifies a request envelope, not plain text. The
        original STARTED ledger separately proves the exact sent text. Native's
        journal owns context inclusion for the actual header/input/entry identity;
        this bounded indexed lookup does not guess a request kind or options.
        """
        files.require_private_directory(self.path.parent)
        with self.path.open("rb") as stream:
            header = SessionEntry.read(stream.readline())
        with NativeContextJournal.open_evidence(files.input_proof) as db:
            try:
                proof = NativeContextJournal.for_input(db, user.require_input_id())
            except NativePiUnavailable as missing:
                raise MalformedRecord(
                    "Native publication has no committed context inclusion"
                ) from missing
            return proof.corroborate(
                self.path.absolute(),
                NativeInputLocation(
                    user.require_input_id(),
                    header.id,
                    user.id.require_present(),
                    session_entry_start=session_entry_start,
                ),
            )

    def inputs_for(self, records: Sequence[NativeRecord], *, floor: int = 0) -> tuple[NativeRecord, ...]:
        """Find the user input behind each record in one reverse walk for the batch.

        Parent IDs are followed back from the latest record; every intervening
        record is visited once for the whole batch, and the walk stops when each
        chain reaches a user boundary or a root. Tool rounds and branches cannot
        lend another input's publication proof.
        """
        found: list[NativeRecord] = [NoNativeInput()] * len(records)
        waiting: dict[str | RootEntry, list[int]] = defaultdict(list)
        latest = 0
        for position, record in enumerate(records):
            entry = record.entry
            if entry is None:
                continue
            if entry.input_boundary:
                found[position] = record
            else:
                waiting[entry.parent_id].append(position)
                latest = max(latest, record.start)
        # A chain that reaches a root (no parent) has no input.
        waiting.pop(RootEntry(), [])
        if waiting:
            for ancestor in self.reverse(latest, floor=floor):
                entry = ancestor.entry
                if entry is None or entry.id not in waiting:
                    continue
                chain = waiting.pop(entry.id)
                if entry.input_boundary:
                    for position in chain:
                        found[position] = ancestor
                else:
                    waiting[entry.parent_id].extend(chain)
                    waiting.pop(RootEntry(), [])
                if not waiting:
                    break
        return tuple(found)

    def input_for(self, record: NativeRecord) -> NativeRecord:
        """Follow the native record's parents to find the user input that started it."""
        return self.inputs_for((record,))[0]

    def publication_input(self, *, after: int, through: int, files: NativeSessionFiles):
        """Return the final producer and its actual tracked input ancestry.

        This bounded reverse walk runs before any BUS/registry lock is acquired.
        A display-tolerant malformed record cannot supply publication evidence.
        """
        for record in self.reverse(through):
            if record.start < after:
                break
            if not record.is_final_publication():
                continue
            user = self.input_for(record)
            entry = user.require_entry().require_tracked_user()
            self.require_publication_context(entry, session_entry_start=user.start, files=files)
            return record, entry
        raise RelationViolationError("Native turn has no final reply after its checkpoint")

    """How far back receipts read: a Pi record carrying tool output is large, so a smaller
    window can hold no turn's end."""

    def voice(self, deal: Speaking) -> Voice:
        """A native thread can receive the question; its own audit carries the answer."""
        return deal

    def tail(self, *, max_bytes: int | None = None):
        end = self.path.stat().st_size
        floor = max(0, end - max_bytes) if max_bytes is not None else 0
        for _, _, raw in reverse_records(self.path, end, floor=floor):
            # Skip only the record still being appended; corrupt history raises.
            if raw.endswith(b"\n"):
                yield NativeEntry.read(raw)

    def forward(self, after: int, through: int):
        with self.path.open("rb") as stream:
            stream.seek(after)
            while stream.tell() < through:
                start = stream.tell()
                raw = stream.readline(through - start)
                if not raw:
                    return
                yield NativeRecord.read(start, stream.tell(), raw)

    def reverse(self, before: int, *, floor: int = 0) -> Generator[NativeRecord]:
        for start, end, raw in reverse_records(self.path, before, floor=floor):
            yield NativeRecord.read(start, end, raw)
