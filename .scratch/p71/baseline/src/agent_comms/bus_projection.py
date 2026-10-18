"""Disposable bus projections borrow an original opened revision, never authority."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, ClassVar, Generic, TypeVar

from .field_codec import FieldCodec
from .private_path import FileRevision
from .store_files import _atomic_write_text, _store_lock


@dataclass(frozen=True)
class AppendCheckpoint:
    """One decoded record's prefix validity against its append-log revision.

    The saved file carries an integrity digest; _read checks it once when the
    bytes are decoded, so an in-memory checkpoint is never re-encoded to check it.
    """

    source: FileRevision
    offset: int
    tail: str

    def __post_init__(self) -> None:
        if not 0 <= self.offset <= self.source.size:
            raise ValueError("Projection offset exceeds its original source")

    @staticmethod
    def fingerprint(stream: BinaryIO, offset: int) -> str:
        start = max(0, offset - 4096)
        stream.seek(start)
        return hashlib.sha256(stream.read(offset - start)).hexdigest()

    @staticmethod
    def payload_digest(payload: dict) -> str:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    def signed_payload(self) -> dict:
        payload = FieldCodec.encode(self)
        return {**payload, "integrity": self.payload_digest(payload)}

    def accepts(self, revision: FileRevision, stream: BinaryIO) -> bool:
        if self.source.identity != revision.identity or self.offset > revision.size:
            return False
        if self.offset == revision.size and self.source != revision:
            return False
        return self.tail == self.fingerprint(stream, self.offset)


Checkpoint = TypeVar("Checkpoint", bound=AppendCheckpoint)


class AppendProjectionStore(Generic[Checkpoint]):
    """Publish a prepared prefix without retaining its source's writer lock.

    AppendCheckpoint owns prefix validity. A projection may lag its source;
    readers validate it again. Serialization happens before the projection lock,
    and publication never rolls a farther valid saved prefix backwards.
    """

    record_type: type[Checkpoint]
    fsync_parent: ClassVar[bool] = False

    def __init__(self, source_path: Path, path: Path):
        self.source_path, self.path = source_path, path

    def _encode(self, record: Checkpoint) -> dict:
        return record.signed_payload()

    def _decode(self, payload: dict) -> Checkpoint:
        integrity = payload.pop("integrity")
        if integrity != AppendCheckpoint.payload_digest(payload):
            raise ValueError("Damaged disposable projection")
        return FieldCodec.decode(self.record_type, payload)

    def _read_unlocked(self) -> Checkpoint | None:
        try:
            return self._decode(json.loads(self.path.read_text()))
        except FileNotFoundError:
            return None
        except (ValueError, TypeError, KeyError, AttributeError):
            return None  # Damaged disposable index: rebuilt from its source.

    def _write_unlocked(self, text: str) -> None:
        _atomic_write_text(self.path, text, fsync_parent=self.fsync_parent)

    def write(self, record: Checkpoint) -> None:
        payload = json.dumps(self._encode(record))
        # A completed historical prefix remains useful after later appends. It
        # may advance the disk index, but never replace a farther valid prefix.
        with _store_lock(self.path):
            try:
                stream = self.source_path.open("rb")
            except FileNotFoundError:
                return
            with stream:
                current = FileRevision.from_stat(os.fstat(stream.fileno()))
                if not record.accepts(current, stream):
                    return
                saved = self._read_unlocked()
                if (
                    saved is not None
                    and saved.accepts(current, stream)
                    and saved.offset > record.offset
                ):
                    return
            self._write_unlocked(payload)


class BusAppendIndex(AppendProjectionStore[Checkpoint]):
    """A bus reader retains one candidate through the shared projection owner."""

    def __init__(self, bus_path: Path, path: Path):
        super().__init__(bus_path, path)
        # The checkpoint this process last accepted or computed. The saved file
        # is read only until there is one; it serves a cold start, not each cut.
        self._held: Checkpoint | None = None

    def prepare(self) -> Checkpoint | None:
        """A candidate before the bus cut is captured and its lock is taken."""
        return self._held if self._held is not None else self._read_unlocked()

    def hold(self, record: Checkpoint) -> None:
        """Keep an exact-cut checkpoint as the next cut's candidate."""
        self._held = record

    def capture(
        self, prepared: Checkpoint | None, stream: BinaryIO | None,
        revision: FileRevision | None,
    ) -> Checkpoint | None:
        if prepared is None or stream is None or revision is None:
            return None
        self._held = prepared if prepared.accepts(revision, stream) else None
        return self._held
