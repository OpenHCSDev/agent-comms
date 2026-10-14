"""Disposable append projections borrow an original cut and never decode under a lock."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, BinaryIO, Generic, TypeVar

from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
from .private_path import FileRevision
from .store_files import _atomic_write_text, _store_lock


class ProjectionCoverage(DeclaredFamily, ABC, affix="Projection"):
    """Whether committed projection rows cover the requested original cut."""

    @abstractmethod
    def complete(self) -> bool:
        """Answer whether this state covers its requested source cut."""

    @abstractmethod
    def resolve(self, committed, behind: Callable[[ProjectionBehind], object]):
        """Use committed data or let the caller read uncovered original bytes."""


@dataclass(frozen=True)
class ProjectionCurrent(ProjectionCoverage):
    def complete(self) -> bool:
        return True

    def resolve(self, committed, behind: Callable[[ProjectionBehind], object]):
        return committed


@dataclass(frozen=True)
class ProjectionBehind(ProjectionCoverage):
    through: int
    size: int

    def __post_init__(self) -> None:
        if not 0 <= self.through < self.size:
            raise ValueError("Behind projection coverage requires an uncovered source suffix")

    def complete(self) -> bool:
        return False

    def resolve(self, committed, behind: Callable[[ProjectionBehind], object]):
        return behind(self)


ProjectedValue = TypeVar("ProjectedValue")


@dataclass(frozen=True)
class ProjectionRead(Generic[ProjectedValue]):
    coverage: ProjectionCoverage
    committed: ProjectedValue

    def resolve(self, behind: Callable[[ProjectionBehind], object]):
        return self.coverage.resolve(self.committed, behind)


@dataclass(frozen=True)
class AppendCheckpoint:
    """One decoded record's prefix validity and integrity policy."""

    source: FileRevision
    offset: int
    tail: str
    integrity: str = field(default="", kw_only=True)

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
        unsigned = {key: value for key, value in payload.items() if key != "integrity"}
        raw = json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    def digest(self) -> str:
        return self.payload_digest(FieldCodec.encode(self))

    def signed_payload(self) -> dict:
        """Encode the projection once for both integrity and atomic persistence."""
        payload = FieldCodec.encode(self)
        payload["integrity"] = self.payload_digest(payload)
        return payload

    def accepts(self, revision: FileRevision, stream: BinaryIO) -> bool:
        if self.integrity != self.digest():
            return False
        if self.source.identity != revision.identity or self.offset > revision.size:
            return False
        if self.offset == revision.size and self.source != revision:
            return False
        return self.tail == self.fingerprint(stream, self.offset)


Checkpoint = TypeVar("Checkpoint", bound=AppendCheckpoint)


@dataclass(frozen=True)
class AppendSegment:
    """One decoded source segment captured without a projection lock."""

    through: int
    size: int
    records: tuple[Mapping[str, Any], ...]
    tail: str | None

    @property
    def coverage(self) -> ProjectionCoverage:
        return BusAppendIndex.coverage(self.through, self.size)


class BusAppendIndex(Generic[Checkpoint]):
    """The shared identity of one disposable projection over an append-only bus."""

    record_type: type[Checkpoint]

    def __init__(self, bus_path: Path, path: Path):
        self.bus_path, self.path = bus_path, path

    @staticmethod
    def coverage(through: int, size: int) -> ProjectionCoverage:
        return ProjectionCurrent() if through >= size else ProjectionBehind(through, size)

    @staticmethod
    def capture_segment(
        source: BinaryIO, *, after: int, size: int, max_bytes: int
    ) -> AppendSegment:
        """Decode one complete bounded append segment without a projection lock."""
        source.seek(after)
        limit = min(size, after + max_bytes)
        records = []
        while source.tell() < size and source.tell() < limit:
            raw = source.readline(size - source.tell())
            if not raw.endswith(b"\n"):
                raise ValueError("Append projection source has an incomplete original row")
            if not raw.strip():
                continue
            record = json.loads(raw)
            if not isinstance(record, Mapping):
                raise ValueError("Append projection source row must be an object")
            records.append(record)
        through = source.tell()
        tail = AppendCheckpoint.fingerprint(source, through) if through else None
        return AppendSegment(through, size, tuple(records), tail)


class JsonCheckpointProjection(Generic[Checkpoint]):
    """Atomic JSON checkpoint capability composed with ``BusAppendIndex``."""

    record_type: type[Checkpoint]
    bus_path: Path
    path: Path

    def _read(self) -> Checkpoint | None:
        try:
            return FieldCodec.decode(self.record_type, json.loads(self.path.read_text()))
        except FileNotFoundError:
            return None
        except (ValueError, TypeError, KeyError):
            return None  # Damaged disposable index: rebuilt from the bus.

    def prepare(self) -> Checkpoint | None:
        """Read a candidate before the bus cut is captured and its lock is taken."""
        return self._read()

    def capture(
        self, prepared: Checkpoint | None, stream: BinaryIO | None,
        revision: FileRevision | None,
    ) -> Checkpoint | None:
        if prepared is None or stream is None or revision is None:
            return None
        if prepared.accepts(revision, stream):
            return prepared
        return None

    def remember(self, record: Checkpoint) -> Checkpoint:
        payload = record.signed_payload()
        return replace(record, integrity=payload["integrity"])

    def write(self, record: Checkpoint, *, fsync_parent: bool = False) -> None:
        retained = self.remember(record)
        payload = retained.signed_payload()
        # Decode/replay happened before this bounded checkpoint commit. The lock
        # protects only validation against a farther committed checkpoint.
        with _store_lock(self.path):
            try:
                stream = self.bus_path.open("rb")
            except FileNotFoundError:
                return
            with stream:
                current = FileRevision.from_stat(os.fstat(stream.fileno()))
                if not retained.accepts(current, stream):
                    return
                saved = self._read()
                if (
                    saved is not None
                    and saved.accepts(current, stream)
                    and saved.offset > retained.offset
                ):
                    return
            _atomic_write_text(self.path, json.dumps(payload), fsync_parent=fsync_parent)


class SQLiteProjection(ABC):
    """WAL writer and lock-free committed reader capability for projections."""

    path: Path

    @property
    @abstractmethod
    def sqlite_tables(self) -> tuple[type, ...]:
        """The declaration-owned tables initialized by this projection."""

    @abstractmethod
    def initialize_projection(self, connection: sqlite3.Connection) -> None:
        """Write this projection's declared initial state."""

    def initialize_sqlite(self) -> None:
        """The writer creates the WAL and schema once, without decoding source bytes."""
        if self.path.exists():
            try:
                with self.sqlite_reader() as current:
                    names = {
                        row[0] for row in current.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                    if all(table.declared_name in names for table in self.sqlite_tables):
                        return
            except sqlite3.DatabaseError:
                pass
        with _store_lock(self.path), closing(sqlite3.connect(self.path, timeout=30)) as connection:
            connection.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("BEGIN IMMEDIATE")
            for table in self.sqlite_tables:
                table.create(connection)
            self.initialize_projection(connection)
            connection.commit()

    def checkpoint_sqlite(self) -> None:
        """Bound the WAL after a writer catches up, outside projection custody."""
        with closing(sqlite3.connect(self.path, timeout=30)) as connection:
            connection.execute("PRAGMA wal_checkpoint(PASSIVE)")

    @contextmanager
    def sqlite_reader(self) -> Iterator[sqlite3.Connection]:
        """Borrow one committed WAL snapshot without a file or writer lock."""
        connection = sqlite3.connect(
            self.path.as_uri() + "?mode=ro", uri=True, timeout=0
        )
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            yield connection
        finally:
            connection.close()
