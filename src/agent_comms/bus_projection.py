"""Disposable bus projections borrow an original opened revision, never authority."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, ClassVar, TypeVar, TYPE_CHECKING
from abc import ABC, abstractmethod

from .field_codec import FieldCodec, MalformedRecord
from .locked_store import JsonDocument, RuntimeFile
from .private_path import FileRevision
from .store_files import _atomic_write_text, _store_lock

if TYPE_CHECKING:
    from .wire_log import WireSnapshot


class WirePrefix(ABC):
    """A complete source prefix; absence has no checkpoint to publish."""

    boundary: int

    def ensure_page_index(self, opened, index, mode) -> None:
        """An empty prefix has no offsets to prepare."""

    @abstractmethod
    def project(self, index, opened, semantics, checkpoint): ...


@dataclass(frozen=True)
class EmptyWirePrefix(WirePrefix):
    boundary: ClassVar[int] = 0

    def project(self, index, opened, semantics, checkpoint):
        return index.record_type.empty(semantics)


@dataclass(frozen=True)
class CompleteWirePrefix(WirePrefix):
    boundary: int

    def ensure_page_index(self, opened, index, mode) -> None:
        opened.ensure_indexed_prefix(index, mode)

    def project(self, index, opened, semantics, checkpoint):
        return index.record_type.from_prefix(index, opened, self.boundary, semantics, checkpoint)


@dataclass(frozen=True)
class NoCheckpoint:
    """No valid saved prefix; consumers obtain their initial projection."""

    def current_at(self, revision):
        return False

    def prepare(self, store):
        return store.read()

    def validated(self, revision, stream):
        return self

    def accepts(self, revision, stream):
        return False

    def ahead_of(self, record, revision, stream):
        return False

    def current_for(self, revision, semantics):
        return False

    def matches_scope(self, semantics):
        return False

    def restore_into(self, destination):
        return 0

    def publish(self, store):
        pass

    def signed_payload(self) -> dict:
        raise TypeError("An absent disposable checkpoint cannot be encoded")


@dataclass(frozen=True)
class AppendCheckpoint:
    """One decoded record's prefix validity against its append-log revision.

    The saved file carries an integrity digest; _read checks it once when the
    bytes are decoded, so an in-memory checkpoint is never re-encoded to check it.
    """

    source: FileRevision
    offset: int
    tail: str

    def current_at(self, revision):
        return self.source == revision

    def prepare(self, store):
        return self

    def validated(self, revision, stream):
        return self if self.accepts(revision, stream) else NoCheckpoint()

    def ahead_of(self, record, revision, stream):
        return self.accepts(revision, stream) and self.offset > record.offset

    def publish(self, store):
        store.write(self)

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
        if not self.source.holds_cut(revision, self.offset):
            return False
        if self.offset == revision.size and not self.current_at(revision):
            return False
        return self.tail == self.fingerprint(stream, self.offset)


Checkpoint = TypeVar("Checkpoint", bound=AppendCheckpoint)


class AppendProjectionStore(RuntimeFile, JsonDocument[Checkpoint | NoCheckpoint]):
    """Publish a prepared prefix without retaining its source's writer lock.

    AppendCheckpoint owns prefix validity. A projection may lag its source;
    readers validate it again. Serialization happens before the projection lock,
    and publication never rolls a farther valid saved prefix backwards. A
    projection is disposable, so no carry writes it.
    """

    record_type: type[Checkpoint]
    fsync_parent: ClassVar[bool] = False

    def __init__(self, source_path: Path, path: Path):
        self.source_path, self.path = source_path, path

    def _encode(self, record: Checkpoint | NoCheckpoint) -> dict:
        return record.signed_payload()

    def _decode(self, payload: object) -> Checkpoint:
        if not isinstance(payload, dict) or "integrity" not in payload:
            raise MalformedRecord("Disposable projection has no integrity digest")
        integrity = payload.pop("integrity")
        if integrity != AppendCheckpoint.payload_digest(payload):
            raise MalformedRecord("Damaged disposable projection")
        return FieldCodec.decode(self.record_type, payload)

    def empty(self) -> NoCheckpoint:
        return NoCheckpoint()

    def _unreadable(self, error: Exception) -> NoCheckpoint:
        return self.empty()  # Disposable index: rebuilt from its source.

    def _replace_unlocked(self, text: str) -> None:
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
                if saved.ahead_of(record, current, stream):
                    return
            self._write_unlocked(payload)


class BusAppendIndex(AppendProjectionStore[Checkpoint]):
    """A bus reader retains one candidate through the shared projection owner."""

    def __init__(self, bus_path: Path, path: Path):
        super().__init__(bus_path, path)
        # The checkpoint this process last accepted or computed. The saved file
        # is read only until there is one; it serves a cold start, not each cut.
        self._held: Checkpoint | NoCheckpoint = NoCheckpoint()

    def prepare(self) -> Checkpoint | NoCheckpoint:
        """A candidate before the bus cut is captured and its lock is taken."""
        return self._held.prepare(self)

    def hold(self, record: Checkpoint) -> None:
        """Keep an exact-cut checkpoint as the next cut's candidate."""
        self._held = record

    def capture(
        self, prepared: Checkpoint | NoCheckpoint, opened: WireSnapshot,
    ) -> Checkpoint | NoCheckpoint:
        self._held = prepared.validated(opened.revision, opened.stream)
        return self._held
