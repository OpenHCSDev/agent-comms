"""Disposable activity projection; the append log remains authoritative."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, ClassVar, Literal

from .activity import Activity
from .bus_projection import AppendCheckpoint, AppendProjectionStore
from .private_path import FileRevision


@dataclass(frozen=True)
class ActivityCheckpoint(AppendCheckpoint):
    latest: dict[str, Activity]
    schema: Literal[1] = 1

    def __post_init__(self) -> None:
        super().__post_init__()
        if any(name != event.thread for name, event in self.latest.items()):
            raise ValueError("Activity checkpoint identity does not match its event")

    def restore_into(self, latest):
        latest.update(self.latest)
        return self.offset

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
class ActivityCheckpointStore(AppendProjectionStore[ActivityCheckpoint]):
    """The shared append projection owns both decoding and publication."""

    path: Path
    source_path: Path
    filename: ClassVar[str] = "activity_latest.json"
    record_type: ClassVar[type[ActivityCheckpoint]] = ActivityCheckpoint
