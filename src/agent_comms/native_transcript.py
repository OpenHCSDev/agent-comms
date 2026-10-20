"""Bounded native file traversal; decode once per visited record, no extra index."""

from __future__ import annotations

from abc import abstractmethod
from collections import defaultdict
from collections.abc import Generator
from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .native_pi import NativeContextProof
    from .read_ledger import ReadLedger
    from .thread_identity import RecordedThreadIncarnation

from .coordination_errors import TranscriptChanged
from .declared_family import DeclaredFamily
from .field_codec import MalformedRecord
from .native_entries import NativeEntry, RecordedEntry, SessionEntry, TranscriptProjection
from .native_runtime_input import NativeRuntimeInput, PublishedReplyRevision
from .private_path import FileRevision
from .recorded_time import EventTime, RecordedEventTime, UnrecordedEventTime
from .transcript_sources import TranscriptRecord, TranscriptSource


def reverse_records(
    path: Path, before: int, *, floor: int = 0, stream=None
) -> Generator[tuple[int, int, bytes], None, None]:
    """Scan offsets in fixed chunks, then read each complete record exactly once.

    Record size is independent of scan-buffer size. In particular, images/tool
    output exceeding a buffer are neither dropped nor repeatedly concatenated.
    """
    with (path.open("rb") if stream is None else nullcontext(stream)) as stream:
        position = end = before
        while position > floor:
            count = min(position - floor, 65536)
            position -= count
            stream.seek(position)
            block = stream.read(count)
            stop = len(block)
            while (boundary := block.rfind(b"\n", 0, stop)) >= 0:
                start = position + boundary + 1
                stop = boundary
                if start == end:
                    continue
                stream.seek(start)
                yield start, end, stream.read(end - start)
                end = start
        if floor == 0 and end:
            stream.seek(0)
            yield 0, end, stream.read(end)


class RecordsCut(DeclaredFamily, affix="Cut"):
    """How far a thread's own records reached when its page was read.

    Members supply ``offset``: the page position just after their last record.
    """

    offset: int

    def holds(self, current: RecordsCut, through: int) -> bool:
        """Whether ``current`` still contains the records this page read through."""
        return type(current) is type(self) and self.contains(current, through)

    @abstractmethod
    def contains(self, current, through: int) -> bool: ...

    @abstractmethod
    def draft_current(self, current: RecordsCut) -> bool:
        """Whether this cut can still accompany the source's current draft."""


class SessionCut(RecordsCut):
    """The saved session's actual file state, including one not written yet."""

    def draft_current(self, current: RecordsCut) -> bool:
        # Native drafts belong to the ACP stream, not this saved-record reader.
        return self.holds(current, self.offset)

    @abstractmethod
    def mark_read(self, reads: ReadLedger, viewer: str, source: str, through: int) -> None: ...

    @abstractmethod
    def publication_revision(
        self, root: Path, reader: NativeTranscript, recipient: RecordedThreadIncarnation
    ) -> PublishedReplyRevision: ...


@dataclass(frozen=True)
class MissingSessionCut(SessionCut):
    offset = 0

    def contains(self, current: SessionCut, through: int) -> bool:
        return through == 0

    def mark_read(self, reads: ReadLedger, viewer: str, source: str, through: int) -> None:
        # No file means no written transcript, just as unread counts return zero.
        pass

    def publication_revision(
        self, root: Path, reader: NativeTranscript, recipient: RecordedThreadIncarnation
    ) -> PublishedReplyRevision:
        return PublishedReplyRevision(0, 0)


@dataclass(frozen=True)
class PresentSessionCut(SessionCut):
    revision: FileRevision

    @property
    def offset(self) -> int:
        return self.revision.size

    def contains(self, current: PresentSessionCut, through: int) -> bool:
        # A session is append-only within one inode; replacement changes the cut.
        return self.revision.holds_cut(current.revision, through)

    def mark_read(self, reads: ReadLedger, viewer: str, source: str, through: int) -> None:
        if not 0 <= through <= self.offset:
            raise TranscriptChanged()
        reads.mark_transcript(viewer, source, self.revision.identity.inode,
                              through, self.revision.size)

    def publication_revision(
        self, root: Path, reader: NativeTranscript, recipient: RecordedThreadIncarnation
    ) -> PublishedReplyRevision:
        return NativeRuntimeInput.publication_revision(root, reader, recipient.lookup)


@dataclass(frozen=True)
class NativeRecord(TranscriptRecord):
    start: int
    end: int
    entry: RecordedEntry | None
    complete: bool

    @classmethod
    def read(cls, start: int, end: int, raw: bytes):
        # Only a record still being appended lacks its newline; a complete
        # record that does not decode is corrupt history and raises.
        complete = raw.endswith(b"\n")
        entry = NativeEntry.read(raw) if complete else None
        return cls(start, end, entry, complete)

    def project(self, projection):
        """A decoded record owns whether it can produce presentation events."""
        return projection(self) if self.entry is not None else ()

    def incomplete_tail(self, through: int) -> bool:
        return self.end == through and not self.complete and self.entry is None

    @property
    def size(self) -> int:
        return self.end - self.start

    @property
    def annotation_id(self) -> str | None:
        entry = self.entry
        return entry.id if entry is not None and entry.is_message else None

    @property
    def events(self):
        return self.project(lambda original: original.entry.events(TranscriptProjection(position=original.start)))

    def publication_producer(self) -> NativeEntry | None:
        if not self.complete or self.entry is None:
            raise MalformedRecord("Native publication has incomplete evidence")
        if self.entry.final_reply:
            self.entry.require_entry_id()
            return self.entry
        return None


class StoredRecord(NativeRecord):
    @property
    def size(self):
        return sum(event.text_size for event in self.events)


class NativeTranscript(TranscriptSource):
    def __init__(self, path: Path):
        self.path = path

    def _tool_call_times(self, since: float | None = None) -> Generator[float]:
        try:
            with closing(self.tail()) as entries:
                for entry in entries:
                    timestamp = entry.timestamp.at.timestamp()
                    if since is not None and timestamp < since:
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

    def tool_call_between(self, since: float, through: float) -> bool:
        with closing(self._tool_call_times(since)) as times:
            return any(timestamp <= through for timestamp in times)

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
        from .session_revision import SessionRevision

        revision = SessionRevision.observe(str(self.path)).require_available()
        with self.path.open("rb") as stream:
            header = NativeEntry.read(stream.readline())
            if not isinstance(header, SessionEntry):
                raise ValueError("Imported source has no original native session header")
            header.require_header()
            sources = []
            for raw in stream:
                entry = NativeEntry.read(raw)
                if entry.is_message:
                    break
                sources.extend(entry.imported_sources())
        if not revision.current(str(self.path)):
            raise ValueError("Imported context saved source changed while reading")
        return tuple(sources)

    def cut(self) -> SessionCut:
        revision = FileRevision.capture(self.path)
        return MissingSessionCut() if revision is None else PresentSessionCut(revision)

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

    @property
    def session_id(self):
        with self.path.open("rb") as stream:
            entry = NativeEntry.read(stream.readline())
        return entry.id if isinstance(entry, SessionEntry) else None

    def annotation_ids(self, after: int, through: int) -> tuple[str, ...]:
        return tuple(identity for record in self.forward(after, through)
                     if (identity := record.annotation_id) is not None)

    def require_publication_context(
        self, user: NativeEntry, *, session_entry_start: int,
    ) -> NativeContextProof:
        """Corroborate the existing native producer's context, without replay.

        Native inputDigest identifies a request envelope, not plain text. The
        original STARTED ledger separately proves the exact sent text. Native's
        journal owns context inclusion for the actual header/input/entry identity;
        this bounded indexed lookup does not guess a request kind or options.
        """
        from .native_pi import NativeContextJournal, _private_session_dir

        _private_session_dir(self.path.parent)
        with self.path.open("rb") as stream:
            header = NativeEntry.read(stream.readline())
        if not isinstance(header, SessionEntry):
            raise MalformedRecord("Native publication has no original session header")
        header.require_header()
        with NativeContextJournal.open_evidence(self.path.absolute()) as db:
            proof = NativeContextJournal.for_input(db, user.input_id)
            if proof is None:
                raise MalformedRecord("Native publication has no committed context inclusion")
            return proof.corroborate(
                self.path.absolute(), header, user, session_entry_start=session_entry_start,
            )

    def inputs_for(self, records) -> tuple:
        """Find the user input behind each record in one reverse walk for the batch.

        Parent IDs are followed back from the latest record; every intervening
        record is visited once for the whole batch, and the walk stops when each
        chain reaches a user boundary or a root. Tool rounds and branches cannot
        lend another input's publication proof.
        """
        found = [None] * len(records)
        waiting: dict[str | None, list[int]] = defaultdict(list)
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
        waiting.pop(None, None)
        if waiting:
            for ancestor in self.reverse(latest):
                entry = ancestor.entry
                if entry is None or entry.id not in waiting:
                    continue
                chain = waiting.pop(entry.id)
                if entry.input_boundary:
                    for position in chain:
                        found[position] = ancestor
                else:
                    waiting[entry.parent_id].extend(chain)
                    waiting.pop(None, None)
                if not waiting:
                    break
        return tuple(found)

    def input_for(self, record: NativeRecord):
        """Follow the native record's parents to find the user input that started it."""
        return self.inputs_for((record,))[0]

    def publication_input(self, *, after: int, through: int):
        """Return the final producer and its actual tracked input ancestry.

        This bounded reverse walk runs before any BUS/registry lock is acquired.
        A display-tolerant malformed record cannot supply publication evidence.
        """
        from .errors import RelationViolationError

        for record in self.reverse(through):
            if record.start < after:
                break
            producer = record.publication_producer()
            if producer is None:
                continue
            user = self.input_for(record)
            if user is None:
                raise RelationViolationError("Native publication has no tracked input ancestry")
            entry = user.entry.require_tracked_user()
            self.require_publication_context(entry, session_entry_start=user.start)
            return record, entry
        raise RelationViolationError("Native turn has no final reply after its checkpoint")

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

    def reverse(self, before: int):
        for start, end, raw in reverse_records(self.path, before):
            yield NativeRecord.read(start, end, raw)
