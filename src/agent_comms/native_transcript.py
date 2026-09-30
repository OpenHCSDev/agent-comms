"""Bounded native file traversal; decode once per visited record, no extra index."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

from .native_entries import NativeEntry, SessionEntry


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


@dataclass(frozen=True)
class NativeRecord:
    start: int
    end: int
    entry: NativeEntry | None
    complete: bool

    @classmethod
    def read(cls, start: int, end: int, raw: bytes):
        try:
            entry = NativeEntry.read(raw)
        except (ValueError, TypeError, UnicodeError):
            entry = None
        return cls(start, end, entry, raw.endswith(b"\n"))

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


class NativeTranscript:
    def __init__(self, path: Path):
        self.path = path

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

    def input_ancestor(self, record: NativeRecord):
        """Follow original parent IDs to the input boundary, without an index.

        The reverse reader visits each intervening record once, with fixed scan
        buffers. A user boundary ends the walk even when it is not tracked.
        Tool rounds and branches cannot lend another input's publication proof.
        """
        parent = record.entry.parent_id
        if parent is None:
            return None
        for ancestor in self.reverse(record.start):
            entry = ancestor.entry
            if entry is None or entry.id != parent:
                continue
            if entry.input_boundary:
                return entry
            parent = entry.parent_id
            if parent is None:
                return None
        return None

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
            user = self.input_ancestor(record)
            if user is None:
                raise RelationViolationError("Native publication has no tracked input ancestry")
            user = user.require_tracked_user()
            self.require_publication_context(user)
            return record, user
        raise RelationViolationError("Native turn has no final reply after its checkpoint")

    def tail(self, *, max_bytes: int | None = None):
        try:
            end = self.path.stat().st_size
            floor = max(0, end - max_bytes) if max_bytes is not None else 0
            for _, _, raw in _reverse_records(self.path, end, floor=floor):
                try:
                    yield NativeEntry.read(raw)
                except (ValueError, TypeError, UnicodeError):
                    continue
        except OSError:
            return

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
