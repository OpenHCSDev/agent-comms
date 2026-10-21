"""The practice bus a rehearsal carries: its copy, private home, size and space."""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from agent_comms.installer.backup import UpgradeRefused
from agent_comms.locked_store import RegistryFile, RootFile, SQLiteFile
from agent_comms.registry_store import RegistryStore
from agent_comms.store_files import SharedLock, _store_lock
from agent_comms.maintenance_barrier import MaintenanceBarrier
from agent_comms.storage_rules import StorageRule


@dataclass(frozen=True)
class PracticeMeasurement:
    bytes: int
    seconds: float

    max_bytes = 3 * 1024**3

    @classmethod
    def of(cls, copy: Path, seconds: float, floor: float) -> PracticeMeasurement:
        """Refuse a copy over its cap or without room for the rehearsal's second copy."""
        size = sum(path.stat().st_size for path in copy.rglob("*") if path.is_file())
        if size > cls.max_bytes:
            raise UpgradeRefused(f"Practice copy is {size} bytes, over its {cls.max_bytes} cap")
        if shutil.disk_usage(copy.parent).free < 2 * size + floor:
            raise UpgradeRefused(f"Insufficient free space for rehearsal of {size} copied bytes")
        return cls(size, seconds)


def prepare_runtime_home(home: Path) -> None:
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    pointer = home / ".local/share/agent-comms/current"
    pointer.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    pointer.unlink(missing_ok=True)
    pointer.symlink_to((Path.home() / ".local/share/agent-comms/current").resolve(strict=True))
    uv = home / ".local/state/agent-comms/upgrade-bus/uv"
    uv.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    uv.unlink(missing_ok=True)
    uv.symlink_to(Path.home() / ".local/state/agent-comms/upgrade-bus/uv")
    cache = Path(subprocess.run(["npm", "config", "get", "cache"], capture_output=True,
                                text=True, check=True).stdout.strip())
    if not (home / ".npm").exists() and cache.is_dir():
        (home / ".npm").symlink_to(cache, target_is_directory=True)
    elif not (home / ".npm").exists():
        (home / ".npm").mkdir(mode=0o700)


def declared_files(root: Path) -> dict[Path, type[RootFile]]:
    """Every existing file a declared owner names, with that owner, never a probe of listed
    files: an undeclared file (a native writer's transient claim, a lock) has no owner, so
    no rehearsal reads it and the copy never meets it."""
    RootFile.load_declarations()
    return {path: owner for owner, directory in RootFile.declarations_in(root)
            for path in owner.matching_in(directory)}


def _copy_database(source: Path, copy: Path) -> None:
    """Copy one database's committed state through SQLite's backup; its companions never."""
    copy.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as original,
          closing(sqlite3.connect(copy)) as copied):
        original.backup(copied)
    shutil.copymode(source, copy)  # SQLite creates the copy under the umask


def _copy_file(source: Path, copy: Path) -> None:
    copy.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _store_lock(source, mode=SharedLock()):
        shutil.copy2(source, copy)


@contextmanager
def practice_copy(root: Path, into: Path) -> Iterator[tuple[Path, PracticeMeasurement]]:
    """Copy the bus as files, so no runtime's code reads it to build the copy: the outgoing
    runtime's bugs cannot block the install of their fix, and the target cannot read a
    format its carries have not run on yet. The copy is the declared owners' files and
    nothing else: every SQLite owner's database copies through SQLite's backup, and its
    -wal, -shm and journal never. The registry's cut_paths copy together under its
    lock; other declared files copy byte for byte under their source-file lock.
    The maintenance witnesses stay behind: the rehearsal holds its copy itself. Stored
    paths keep naming the live bus, and every step writes only inside the root it is given."""
    floor = StorageRule.load(root).floor_gib * 2**30
    into.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if shutil.disk_usage(into.parent).free < floor:
        raise UpgradeRefused("Insufficient free space before building the practice copy")
    started = time.monotonic()
    witnesses = set(MaintenanceBarrier(RegistryFile.path_in(root)).witness_paths)
    registry = RegistryStore.at(root)
    files = declared_files(root)
    into.mkdir(mode=0o700)

    def copy(path: Path) -> None:
        owner = files.get(path, RootFile)
        (_copy_database if issubclass(owner, SQLiteFile) else _copy_file)(
            path, into / path.relative_to(root))

    # SQLite backup keeps bus rows and every capability projection at one cut.
    # The registry still needs its original guard and turn files under one hold.
    copied: set[Path] = set()
    with registry.locked(mode=SharedLock()):
        for path in registry.cut_paths:
            if path.exists():
                copy(path)
                copied.add(path)
    for path in files.keys() - copied - witnesses:
        copy(path)
    yield into, PracticeMeasurement.of(into, time.monotonic() - started, floor)
