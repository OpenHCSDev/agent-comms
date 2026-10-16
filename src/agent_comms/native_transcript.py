"""Bounded native file traversal; decode once per visited record, no extra index."""

from __future__ import annotations

from collections import defaultdict
from abc import abstractmethod
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .thread_identity import RecordedThreadIncarnation

from .declared_family import DeclaredFamily
from .native_entries import NativeEntry, SessionEntry
from .private_path import FileRevision
from .transcript_receipts import TranscriptSource


def _reverse_records(
    path: Path, before: int, *, floor: int = 0
) -> Generator[tuple[int, int, bytes], None, None]:
    """Scan offsets in fixed chunks, then read each complete record exactly once.

    Record size is independent of scan-buffer size. In particular, images/tool
    output exceeding a buffer are neither dropped nor repeatedly concatenated.
    """
    with path.open("rb") as stream:
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


@dataclass(frozen=True)
class SessionCut(RecordsCut):
    revision: FileRevision | None  # None until the native session file exists

    @property
    def offset(self) -> int:
        return self.revision.size if self.revision is not None else 0

    def contains(self, current: SessionCut, through: int) -> bool:
        # A session is append-only within one inode: later bytes belong to the
        # next update. Replacement, truncation or a rewrite changes the cut.
        if self.revision is None or current.revision is None:
            return self.revision == current.revision
        return self.revision.holds_cut(current.revision, through)

    def draft_current(self, current: RecordsCut) -> bool:
        # Native drafts belong to the ACP stream, not this saved-record reader.
        # Appends do not invalidate the already captured page's absent draft.
        return self.holds(current, self.offset)


@dataclass(frozen=True)
class NativeRecord:
    start: int
    end: int
    entry: NativeEntry | None
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

    def publication_producer(self) -> NativeEntry | None:
        if not self.complete or self.entry is None:
            raise ValueError("Native publication has incomplete evidence")
        if self.entry.final_reply:
            self.entry.require_entry_id()
            return self.entry
        return None


class NativeTranscript(TranscriptSource):
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def reading(self):
        """Keep the native record owner bound across one page projection."""
        yield self

    def current_draft(self) -> None:
        """Native in-progress output belongs to the ACP stream."""
        return None

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
        return SessionCut(FileRevision.capture(self.path))

    def publication_revision(self, root: Path, recipient: RecordedThreadIncarnation):
        from .native_runtime_input import NativeRuntimeInput, PublishedReplyRevision

        if not self.path.is_file():
            return PublishedReplyRevision(0, 0)
        return NativeRuntimeInput.publication_revision(root, self, recipient.lookup)

    @contextmanager
    def projection(self, receipts, routes):
        """Project records through their native input and the session's routes."""
        with routes.for_session(str(self.path)) as session_routes:
            def project(records, *, max_records, max_bytes):
                return receipts.native_records(
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

    def require_publication_context(self, user):
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
            raise ValueError("Native publication has no original session header")
        header.require_header()
        with NativeContextJournal.open_evidence(self.path.absolute()) as db:
            proof = NativeContextJournal.for_input(db, user.input_id)
            if proof is None:
                raise ValueError("Native publication has no committed context inclusion")
            return proof.corroborate(self.path.absolute(), header, {user.input_id: user})

    def inputs_for(self, records) -> tuple:
        """Each record's original input, found in one reverse walk for the batch.

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
                found[position] = entry
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
                        found[position] = entry
                else:
                    waiting[entry.parent_id].extend(chain)
                    waiting.pop(None, None)
                if not waiting:
                    break
        return tuple(found)

    def input_for(self, record: NativeRecord):
        """The native record and its parent chain own the original input."""
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
            user = user.require_tracked_user()
            self.require_publication_context(user)
            return record, user
        raise RelationViolationError("Native turn has no final reply after its checkpoint")

    def tail(self, *, max_bytes: int | None = None):
        end = self.path.stat().st_size
        floor = max(0, end - max_bytes) if max_bytes is not None else 0
        for _, _, raw in _reverse_records(self.path, end, floor=floor):
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
        for start, end, raw in _reverse_records(self.path, before):
            yield NativeRecord.read(start, end, raw)
