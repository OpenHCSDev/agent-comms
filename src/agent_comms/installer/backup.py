"""The update owner's snapshots: whole buses or declared store files."""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from collections.abc import Collection, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache
from pathlib import Path
from string import Formatter
from typing import ClassVar, Iterator, Self

from agent_comms.active_route import ActiveRoute, _route_directory, route_directory
from agent_comms.coordination_errors import CoordinationError
from agent_comms.external_transcript import ExternalTranscript
from agent_comms.field_codec import FieldCodec
from agent_comms.installer.interpreter_operation import HoldTranscriptsProbe, RouteSelection
from agent_comms.locked_store import LockedStore, RootFile, RuntimeFile, SQLiteFile
from agent_comms.private_path import FileObservation, FileRevision, MissingFile, fsync_directory
from agent_comms.store_files import AtomicFile


class UpgradeRefused(CoordinationError):
    """The update stopped without permission to discard its original data."""
    prefix: ClassVar[str] = "install: refused:"

    def __str__(self) -> str:
        return f"{self.prefix} {super().__str__()}"

    def lines(self) -> tuple[str, ...]:
        return (f"Install failed: {self}",)


@dataclass(frozen=True)
class SavedFile:
    size: int
    mtime_ns: int
    inode: int

    @classmethod
    def at(cls, path: Path) -> Self:
        observed = path.stat()
        return cls(observed.st_size, observed.st_mtime_ns, observed.st_ino)

    def matches_extraction(self, copied: SavedFile) -> bool:
        """Extraction allocates a new inode and tar loses nanosecond mtime; compare size only."""
        return self.size == copied.size


def bus_files(root: Path) -> dict[str, SavedFile]:
    """Read saved file identity and modification metadata, never contents."""
    excluded = RuntimeFile.paths_in(root)
    saved = {}
    for path in root.rglob("*"):
        if path in excluded or path.name.endswith((".lock", "-shm", "-journal")):
            continue
        observed = path.lstat()
        # Empty WAL files appear on a read-only SQLite open; they hold no committed frames.
        if path.name.endswith("-wal") and observed.st_size == 0:
            continue
        if stat.S_ISREG(observed.st_mode):
            saved[str(path.relative_to(root))] = SavedFile(
                observed.st_size, observed.st_mtime_ns, observed.st_ino)
    return saved


@dataclass(frozen=True)
class BackupReport:
    """Where a live install kept the files it may restore, and their size."""

    backup: str
    bytes: int


@dataclass(frozen=True)
class RestoreSpace:
    bytes: int
    free: int
    seconds: float

    @property
    def needed(self) -> int:
        return self.bytes + max(self.bytes // 10, 1024**3)

    @classmethod
    def measure(cls, rewritten_bytes: int, filesystem: Path) -> Self:
        started = time.perf_counter()
        free = shutil.disk_usage(filesystem).free
        return cls(rewritten_bytes, free, time.perf_counter() - started)

    def require(self) -> None:
        if self.free < self.needed:
            raise UpgradeRefused(
                f"Restore needs {self.needed} bytes on the bus filesystem; "
                f"only {self.free} bytes are free")


@contextmanager
def transcripts_held(root: Path) -> Iterator[None]:
    """Hold SQLite's writer lock in another process while backup files are read."""
    path = ExternalTranscript.path_in(root)
    if not path.exists():
        yield
        return
    ready, held = os.pipe()
    with tempfile.TemporaryDirectory() as scratch:
        holder = subprocess.Popen(
            HoldTranscriptsProbe(path, held).command(
                Path(sys.executable), Path(scratch) / "result.json"),
            stdin=subprocess.PIPE, pass_fds=(held,),
            env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
        os.close(held)
        try:
            with open(ready, "rb") as holding:
                if not holding.read():
                    raise UpgradeRefused(f"Could not hold the transcript store {path}")
            yield
        finally:
            holder.communicate()


@dataclass(frozen=True, order=True)
class BackupName:
    """One fixed-width name declaration owns creation, recognition and ordering."""

    value: str
    format: ClassVar[str] = (
        "{root}.upgrade-{created.year:04d}{created.month:02d}{created.day:02d}"
        "T{created.hour:02d}{created.minute:02d}{created.second:02d}"
    )

    @classmethod
    def now(cls, root: Path) -> Self:
        return cls(cls.format.format(root=root.name, created=datetime.now()))

    @classmethod
    @cache
    def pattern(cls, root: Path) -> re.Pattern[str]:
        parts = []
        for literal, name, spec, _ in Formatter().parse(cls.format):
            parts.append(re.escape(literal))
            if name:
                parts.append(rf"[0-9]{{{int(spec.removesuffix('d'))}}}" if spec
                             else re.escape(root.name))
        return re.compile("".join(parts))

    @classmethod
    def matching(cls, root: Path, names: Iterable[str]) -> Iterator[Self]:
        pattern = cls.pattern(root)
        for name in names:
            if pattern.fullmatch(name):
                yield cls(name)


class ResourceBackup(ABC):
    """The installation's rollback and retention behavior, with or without a snapshot."""

    @property
    @abstractmethod
    def description(self) -> str: ...

    @abstractmethod
    def report(self) -> BackupReport: ...

    @abstractmethod
    def original(self) -> bool: ...

    @abstractmethod
    def restore(self) -> None: ...

    @abstractmethod
    def retain(self, spill_dir: Path) -> tuple[Path, ...]: ...

    @abstractmethod
    def spill_directory(self, configured: Path) -> Path:
        """Carry originals share the snapshot's retention; no snapshot creates nothing."""


@dataclass
class StoreBackup(ResourceBackup):
    """Snapshot selected stores and the route, including its original absence."""
    root: Path
    route: Path
    path: Path
    kept: ClassVar[int] = 2
    route_revision: FileObservation = field(default_factory=MissingFile, init=False)

    def spill_directory(self, configured: Path) -> Path:
        return self.path

    def files(self) -> dict[str, str]:
        return FieldCodec.decode(dict[str, str],
                                 FieldCodec.parse((self.path / 'files.json').read_text()))

    def retire(self) -> Path:
        archived = self.path.with_name(f'{self.path.name}.restored-{time.time_ns()}')
        self.path.rename(archived)
        fsync_directory(archived.parent)
        print(f'Restored data backup kept at {archived}', flush=True)
        return archived

    def restore_route(self) -> None:
        with route_directory(self.route) as directory:
            if FileRevision.capture(self.route) != self.route_revision:
                print(f'Keeping concurrently changed route: {self.route}', file=sys.stderr)
                return
            if self.route_original():
                return
            saved = self.path / 'active-route.json'
            if saved.exists():
                AtomicFile(self.route).write_text(saved.read_text())
            else:
                os.unlink(self.route.name, dir_fd=directory)
                os.fsync(directory)
            self.route_revision = FileRevision.capture(self.route)
            if not self.route_original():
                raise UpgradeRefused('Active route differs after backup restoration')

    def require_route_revision(self) -> None:
        if FileRevision.capture(self.route) != self.route_revision:
            raise UpgradeRefused('Active route changed after its backup')

    def rebind_route(self, route: ActiveRoute, selected: RouteSelection) -> str:
        with _route_directory(route, self.route) as (path, directory):
            self.require_route_revision()
            try:
                return selected.publish(route, path, directory)
            finally:
                # Observe our mutation while EX still excludes another publisher,
                # including a replacement whose durability acknowledgement failed.
                self.route_revision = FileRevision.capture(path)

    def route_original(self) -> bool:
        saved = self.path / 'active-route.json'
        if not saved.exists():
            return not self.route.exists()
        if not self.route.exists():
            return False
        current, original = self.route.stat(), saved.stat()
        return (current.st_mode == original.st_mode and current.st_nlink == original.st_nlink
                and self.route.read_bytes() == saved.read_bytes())

    @classmethod
    def take(cls, root: Path, route: Path, files: tuple[Path, ...], spill_dir: Path) -> StoreBackup:
        path = spill_dir / BackupName.now(root).value
        backup = cls(root, route, path)
        selected = tuple(str(file.relative_to(root)) for file in files)
        if path.exists():
            if tuple(json.loads((path / 'selected.json').read_text())) != selected:
                raise UpgradeRefused('Existing backup belongs to different resources')
            backup.files()  # An interrupted snapshot is never treated as complete.
            with route_directory(route):
                if not backup.route_original():
                    raise UpgradeRefused('Existing backup belongs to a different route')
                backup.route_revision = FileRevision.capture(route)
            return backup
        path.mkdir(mode=0o700, parents=True)
        AtomicFile(path / 'selected.json').write_text(json.dumps(selected))
        contents = backup.capture(files)
        with route_directory(route):
            if route.exists():
                shutil.copy2(route, path / 'active-route.json')
            backup.route_revision = FileRevision.capture(route)
        AtomicFile(path / 'files.json').write_text(json.dumps(FieldCodec.encode(contents), indent=1))
        return backup

    def retain(self, spill_dir: Path) -> tuple[Path, ...]:
        backups = sorted(BackupName.matching(
            self.root, (path.name for path in spill_dir.iterdir() if path.is_dir()),
        ))
        retired = tuple(spill_dir / name.value for name in backups[:-self.kept])
        for path in retired:
            shutil.rmtree(path)
        return retired

    def report(self) -> BackupReport:
        return BackupReport(self.description, sum(path.stat().st_size for path in self.path.rglob('*')
                                                  if path.is_file()))

    @property
    def description(self) -> str:
        return str(self.path)

    @property
    def data_root(self) -> Path:
        return self.path / 'stores'

    @staticmethod
    def digest(path: Path) -> str:
        return RootFile.content_owner(path).digest(path)

    @staticmethod
    def copy_file(source: str | Path, target: str | Path) -> Path:
        """Copy one store through its SQLite snapshot, never through live WAL bytes."""
        source, target = Path(source), Path(target)
        owner = RootFile.content_owner(source)
        owner.copy_file(source, target)
        return target

    @classmethod
    def copy_tree(
        cls, source: Path, target: Path, *, excluded: Collection[Path] = ()
    ) -> None:
        """Copy committed files; their owners declare transient names before any stat."""

        def transient(directory: str, names: list[str]) -> set[str]:
            omitted: set[str] = set()
            for name in sorted(names):
                path = Path(directory) / name
                if name in omitted or path in excluded or LockedStore.is_staging(path):
                    omitted.add(name)
                    continue
                if stat.S_ISSOCK(path.lstat().st_mode):
                    omitted.add(name)
                elif path.is_file() and not path.is_symlink():
                    owner = RootFile.content_owner(path)
                    omitted.update(companion.name for companion in owner.files(path)[1:])
            return omitted

        shutil.copytree(
            source, target, symlinks=True, ignore=transient, copy_function=cls.copy_file
        )

    def capture(self, files: tuple[Path, ...]) -> dict[str, str]:
        contents = {}
        for source in files:
            if not source.exists():
                continue
            name = str(source.relative_to(self.root))
            target = self.data_root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            self.copy_file(source, target)
            with target.open('rb') as ready:
                os.fsync(ready.fileno())
            contents[name] = self.digest(target)
            fsync_directory(target.parent)
        return contents

    def file_original(self, name: str, saved: dict[str, str]) -> bool:
        """An unchanged selected file keeps its identity as well as its contents."""
        target = self.root / name
        if name not in saved:
            return not target.exists()
        return target.exists() and self.digest(target) == saved[name]

    def original(self) -> bool:
        return self.route_original() and self.stores_original()

    def stores_original(self) -> bool:
        if not (self.path / 'files.json').exists():
            return False
        saved = self.files()
        return all(self.file_original(name, saved)
                   for name in json.loads((self.path / 'selected.json').read_text()))

    def restore(self) -> None:
        saved = self.files()
        for name in json.loads((self.path / 'selected.json').read_text()):
            if self.file_original(name, saved):
                continue
            target = self.root / name
            if name in saved:
                descriptor, temporary = tempfile.mkstemp(prefix='.restore-', dir=target.parent)
                os.close(descriptor)
                stage = Path(temporary)
                try:
                    shutil.copy2(self.data_root / name, stage)
                    AtomicFile(target).replace(stage)
                finally:
                    stage.unlink(missing_ok=True)
            else:
                target.unlink(missing_ok=True)
            for companion in SQLiteFile.files(target)[1:]:
                companion.unlink(missing_ok=True)
        fsync_directory(self.root)
        self.restore_route()
        if not self.stores_original():
            raise UpgradeRefused('Selected stores differ after backup restoration')
