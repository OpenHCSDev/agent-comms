"""Disposable activity projection; the append log remains authoritative."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal

from .activity import Activity
from .locked_store import LockedStore
from .private_path import FileRevision


@dataclass(frozen=True, slots=True)
class ActivityCheckpoint:
    source: FileRevision
    offset: int
    tail: str
    latest: dict[str, Activity]
    schema: Literal[1] = 1

    def __post_init__(self) -> None:
        if self.offset < 0:
            raise ValueError("Activity checkpoint offset cannot be negative")
        if any(name != event.thread for name, event in self.latest.items()):
            raise ValueError("Activity checkpoint identity does not match its event")

    @staticmethod
    def tail_digest(log: Path, offset: int) -> str:
        with log.open("rb") as stream:
            start = max(0, offset - 4096)
            stream.seek(start)
            return hashlib.sha256(stream.read(offset - start)).hexdigest()

    def matches(self, log: Path, revision: FileRevision) -> bool:
        return (
            self.source.holds_cut(revision, self.offset)
            and self.tail == self.tail_digest(log, self.offset)
        )

    @classmethod
    def capture(
        cls,
        log: Path,
        revision: FileRevision,
        latest: dict[str, Activity],
        offset: int,
    ) -> ActivityCheckpoint:
        return cls(revision, offset, cls.tail_digest(log, offset), latest)


@dataclass(frozen=True, slots=True)
class ActivityCheckpointStore(LockedStore[ActivityCheckpoint | None]):
    filename: ClassVar[str] = "activity_latest.json"

    @property
    def record_type(self) -> type[ActivityCheckpoint | None]:
        return ActivityCheckpoint | None

    def empty(self) -> None:
        return None

    def _unreadable(self, error: Exception) -> None:
        # Never recover authoritative log data from an invalid derived cache.
        return None
