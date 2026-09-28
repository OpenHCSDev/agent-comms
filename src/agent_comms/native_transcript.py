"""Bounded native file traversal; decode once per visited record, no extra index."""

from __future__ import annotations
from collections.abc import Generator, Iterator
from dataclasses import dataclass
from pathlib import Path
from .native_entries import NativeEntry


def _reverse_lines(path: Path, *, max_bytes: int | None = None) -> Iterator[bytes]:
    """Read JSONL newest-first without allocating the file or oversized lines."""
    try:
        with path.open("rb") as stream:
            position = stream.seek(0, 2)
            floor = max(0, position - max_bytes) if max_bytes is not None else 0
            pending = b""
            oversized = False
            while position > floor:
                count = min(65536, position - floor)
                position -= count
                stream.seek(position)
                parts = (stream.read(count) + pending).split(b"\n")
                pending = parts.pop(0)
                for line in reversed(parts):
                    if oversized:
                        oversized = False
                        continue
                    if line:
                        yield line
                if len(pending) > 256 * 1024:
                    pending = b""
                    oversized = True
            if floor == 0 and pending and not oversized:
                yield pending
    except OSError:
        return


def _reverse_records(path: Path, before: int) -> Generator[tuple[int, int, bytes], None, None]:
    """Seek from the end in chunks; never parse or allocate the preceding history."""
    with path.open("rb") as stream:
        position = before
        end = before
        pending = b""
        while position:
            count = min(position, 65536)
            position -= count
            stream.seek(position)
            pending = stream.read(count) + pending
            while (boundary := pending.rfind(b"\n", 0, len(pending) - 1)) >= 0:
                start = position + boundary + 1
                yield start, end, pending[boundary + 1 :]
                pending, end = pending[: boundary + 1], start
        if pending:
            yield 0, end, pending


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

    @property
    def size(self) -> int:
        return self.end - self.start


class NativeTranscript:
    def __init__(self, path: Path):
        self.path = path

    def tail(self, *, max_bytes: int | None = None):
        for raw in _reverse_lines(self.path, max_bytes=max_bytes):
            try:
                yield NativeEntry.read(raw)
            except (ValueError, TypeError, UnicodeError):
                continue

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
