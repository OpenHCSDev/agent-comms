"""Disposable bus projections borrow an original opened revision, never authority."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Generic, TypeVar

from .field_codec import FieldCodec
from .private_path import FileRevision
from .store_files import _atomic_write_text, _store_lock

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class AppendCheckpoint:
    """One decoded record's prefix validity and source-boundary policy."""

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

    def accepts(self, revision: FileRevision, stream: BinaryIO) -> bool:
        if self.source.identity != revision.identity or self.offset > revision.size:
            return False
        if self.offset == revision.size and self.source != revision:
            return False
        return self.tail == self.fingerprint(stream, self.offset)


Checkpoint = TypeVar("Checkpoint", bound=AppendCheckpoint)


class PreparedCheckpoint(ABC, Generic[Checkpoint]):
    """One named outcome of reading a disposable append index."""

    @abstractmethod
    def capture(
        self, owner: BusAppendIndex, stream: BinaryIO | None, revision: FileRevision | None
    ) -> Checkpoint | None: ...


@dataclass(frozen=True)
class AbsentIndex(PreparedCheckpoint[Checkpoint]):
    """The disposable projection has not been written."""

    def capture(
        self, owner: BusAppendIndex, stream: BinaryIO | None, revision: FileRevision | None
    ) -> None:
        return None


@dataclass(frozen=True)
class DamagedIndex(PreparedCheckpoint[Checkpoint]):
    """The disposable projection exists but cannot be trusted or decoded."""

    path: Path
    reason: str

    @classmethod
    def report(cls, path: Path, error: Exception) -> DamagedIndex:
        damaged = cls(path, str(error))
        _LOG.warning("Rebuilding damaged disposable bus index %s: %s", path, damaged.reason)
        return damaged

    def capture(
        self, owner: BusAppendIndex, stream: BinaryIO | None, revision: FileRevision | None
    ) -> None:
        return None


@dataclass(frozen=True)
class LoadedIndex(PreparedCheckpoint[Checkpoint]):
    """A decoded projection candidate; the opened bus cut still validates it."""

    record: Checkpoint

    def capture(
        self, owner: BusAppendIndex, stream: BinaryIO | None, revision: FileRevision | None
    ) -> Checkpoint | None:
        if stream is not None and revision is not None and self.record.accepts(revision, stream):
            return self.record
        return None


class BusAppendIndex(Generic[Checkpoint]):
    """One strict record decoder and atomic writer for disposable projections."""

    record_type: type[Checkpoint]

    def __init__(self, bus_path: Path, path: Path):
        self.bus_path, self.path = bus_path, path

    def _read(self) -> PreparedCheckpoint[Checkpoint]:
        try:
            payload = json.loads(self.path.read_text())
        except FileNotFoundError:
            return AbsentIndex()
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            return DamagedIndex.report(self.path, error)
        try:
            return LoadedIndex(FieldCodec.decode(self.record_type, payload))
        except (ValueError, TypeError, KeyError) as error:
            return DamagedIndex.report(self.path, error)

    def prepare(self) -> PreparedCheckpoint[Checkpoint]:
        """Read a candidate before the bus cut is captured and its lock is taken."""
        return self._read()

    def capture(
        self, prepared: PreparedCheckpoint[Checkpoint], stream: BinaryIO | None,
        revision: FileRevision | None,
    ) -> Checkpoint | None:
        return prepared.capture(self, stream, revision)

    def remember(self, record: Checkpoint) -> PreparedCheckpoint[Checkpoint]:
        return LoadedIndex(record)

    def write(self, record: Checkpoint, *, fsync_parent: bool = False) -> None:
        # A completed historical prefix remains useful after later appends. It
        # may advance the disk index, but never replace a farther valid prefix.
        with _store_lock(self.path):
            try:
                stream = self.bus_path.open("rb")
            except FileNotFoundError:
                return
            with stream:
                current = FileRevision.from_stat(os.fstat(stream.fileno()))
                if not record.accepts(current, stream):
                    return
                if record.offset < current.size:
                    saved = self._read().capture(self, stream, current)
                    if saved is not None and saved.offset > record.offset:
                        return
            payload = json.dumps(FieldCodec.encode(record))
            _atomic_write_text(self.path, payload, fsync_parent=fsync_parent)
