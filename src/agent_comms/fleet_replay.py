"""Private fleet snapshots for replaying Core's own standing rules."""

from __future__ import annotations

import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from .field_codec import PathText
from .active_route import LocalRoute
from .commander_heartbeat import CommanderObservation
from .comms import Comms
from .launch_runtime import LaunchRuntime
from .locked_store import RegistryFile, RootFile
from .maintenance_barrier import MaintenanceBarrier
from .root_runtime import RootRuntime
from .storage_rules import StorageRule


@dataclass(frozen=True)
class FleetRootCopy:
    source: Annotated[Path, PathText]
    root: Annotated[Path, PathText]
    captured_at: float
    bytes: int

    @classmethod
    def capture(cls, source: Path, into: Path) -> FleetRootCopy:
        """Read source files only; SQLite owners use their read-only backup path."""
        barrier = MaintenanceBarrier(RegistryFile.path_in(source))
        before = barrier._current_unlocked()
        before.require_open()
        floor = StorageRule.load(source).floor_gib * 1024 ** 3
        paths = tuple(path for path in source.rglob("*")
                      if stat.S_ISREG(path.lstat().st_mode))
        size = sum(path.stat().st_size for path in paths)
        into.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if shutil.disk_usage(into.parent).free < floor + size:
            raise ValueError("Fleet copy would consume the bus's free-space floor")
        into.mkdir(mode=0o700)
        witnesses = frozenset(barrier.witness_paths)
        copied = 0
        for resource, owner in RootFile.owned_files(source, paths).items():
            original = source / resource.path
            if original in witnesses:
                continue
            target = into / resource.path
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            owner.copy_file(original, target)
            target.chmod(0o600)
            copied += target.stat().st_size
        after = barrier._current_unlocked()
        after.require_open()
        if after != before:
            raise ValueError("Maintenance changed during fleet capture; discard this copy")
        RootRuntime.publish(into, LaunchRuntime.running())
        return cls(source, into, time.time(), copied)

    def observations(self) -> tuple[CommanderObservation, ...]:
        comms = Comms(LocalRoute(self.root))
        project = comms.registry.store.read().commander_project
        return tuple(CommanderObservation.read(comms, commander)
                     for commander in project.commanders(comms.registry.snapshot()))
