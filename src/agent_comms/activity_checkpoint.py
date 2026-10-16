"""Disposable activity projection; the append log remains authoritative."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, ClassVar, Literal

from .activity import Activity
from .bus_projection import AppendCheckpoint, AppendProjectionStore
from .locked_store import JsonDocument
from .private_path import FileRevision


@dataclass(frozen=True)
class ActivityCheckpoint(AppendCheckpoint):
    latest: dict[str, Activity]
    schema: Literal[1] = 1

    def __post_init__(self) -> None:
        super().__post_init__()
        if any(name != event.thread for name, event in self.latest.items()):
            raise ValueError("Activity checkpoint identity does not match its event")

    @classmethod
    def capture(
        cls,
        stream: BinaryIO,
        revision: FileRevision,
        latest: dict[str, Activity],
        offset: int,
    ) -> ActivityCheckpoint:
        return cls(revision, offset, cls.fingerprint(stream, offset), latest)


@dataclass(frozen=True, slots=True)
class ActivityCheckpointStore(
    JsonDocument[ActivityCheckpoint | None], AppendProjectionStore[ActivityCheckpoint]
):
    """Keep the established activity document codec and shared prefix publisher."""

    source_path: Path
    filename: ClassVar[str] = "activity_latest.json"

    @property
    def record_type(self) -> type[ActivityCheckpoint | None]:
        return ActivityCheckpoint | None

    def empty(self) -> None:
        return None

    def _unreadable(self, error: Exception) -> None:
        # Never recover authoritative log data from an invalid derived cache.
        return None
