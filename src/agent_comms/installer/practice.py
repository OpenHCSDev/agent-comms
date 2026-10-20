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

from agent_comms.coordination_database import CoordinationStore
from agent_comms.installer.backup import UpgradeRefused, bus_files
from agent_comms.locked_store import RegistryFile
from agent_comms.private_registry_guard import PrivateRegistryGuard
from agent_comms.registry_store import RegistryStore
from agent_comms.store_files import SharedLock, _store_lock
from agent_comms.maintenance_barrier import MaintenanceBarrier
from agent_comms.storage_rules import StorageRule
from agent_comms.wire_log import WireLog


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


@contextmanager
def practice_copy(root: Path, into: Path) -> Iterator[tuple[Path, PracticeMeasurement]]:
    """Copy the bus as files, so no runtime's code reads it to build the copy: the outgoing
    runtime's bugs cannot block the install of their fix, and the target cannot read a
    format its carries have not run on yet. SQLite stores copy through SQLite's backup, the
    log under its own lock, the registry and its guard together under the registry's read
    lock, every other saved file byte for byte. The maintenance witnesses
    stay behind: the rehearsal holds its copy itself. Stored paths keep naming the live bus,
    and every step writes only inside the root it is given."""
    floor = StorageRule.load(root).floor_gib * 2**30
    into.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if shutil.disk_usage(into.parent).free < floor:
        raise UpgradeRefused("Insufficient free space before building the practice copy")
    started = time.monotonic()
    witnesses = {str(path.relative_to(root))
                 for path in MaintenanceBarrier(RegistryFile.path_in(root)).witness_paths}
    log = WireLog(WireLog.path_in(root))
    registry = RegistryStore.at(root)
    saved = bus_files(root)
    databases = {name for name in saved if CoordinationStore.is_database(root / name)}
    into.mkdir(mode=0o700)
    # The log's own file lock, never WireLog.locked(): that checks the index with the
    # target's code, which refuses a bus whose carries have not run yet.
    locked_copies = ((_store_lock(log.path), (log.path, log.metadata_path)),
                     (registry.locked(mode=SharedLock()),
                      (registry.path, PrivateRegistryGuard.path_in(root))))
    for lock, paths in locked_copies:
        with lock:
            for path in paths:
                if path.exists():
                    shutil.copy2(path, into / path.name)
    copied = {path.name for _, paths in locked_copies for path in paths}
    for name in saved.keys() - witnesses - copied:
        source, copy = root / name, into / name
        copy.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if name in databases:
            with (closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as original,
                  closing(sqlite3.connect(copy)) as copied):
                original.backup(copied)
            shutil.copymode(source, copy)  # SQLite creates the copy under the umask
        elif name.removesuffix("-wal") not in databases:
            shutil.copy2(source, copy)
    yield into, PracticeMeasurement.of(into, time.monotonic() - started, floor)
