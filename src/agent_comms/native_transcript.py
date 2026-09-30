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


class NativeTranscript:
    def __init__(self, path: Path):
        self.path = path

    @property
    def session_id(self):
        with self.path.open("rb") as stream:
            entry = NativeEntry.read(stream.readline())
        return entry.id if isinstance(entry, SessionEntry) else None

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
