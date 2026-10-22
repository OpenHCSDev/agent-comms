"""The update owner's snapshots: whole buses or declared store files."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from collections.abc import Collection, Iterable
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from pathlib import Path
from string import Formatter
from typing import ClassVar, Iterator, Self

from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_errors import CoordinationError
from agent_comms.external_transcript import ExternalTranscript
from agent_comms.field_codec import FieldCodec
from agent_comms.installer.outcome import InstallFailure
from agent_comms.locked_store import LockedStore, RuntimeFile
from agent_comms.store_files import _atomic_write_text

HOLD_TRANSCRIPTS = """import sys
from contextlib import closing
from agent_comms.coordination_database import CoordinationStore
with closing(CoordinationStore.writer(sys.argv[1], isolation_level=None)) as writer:
    writer.execute("BEGIN IMMEDIATE")
    with open(int(sys.argv[2]), "wb") as held:
        held.write(b"held")
    sys.stdin.read()
    writer.execute("ROLLBACK")
"""


class UpgradeRefused(InstallFailure, CoordinationError):
    """The update stopped without permission to discard its original data."""
    prefix: ClassVar[str] = "install: refused:"

    def __str__(self) -> str:
        return f"{self.prefix} {super().__str__()}"

    def lines(self) -> tuple[str, ...]:
        return (f"Install failed: {self}",)

    def exception(self) -> BaseException:
        return self

    @classmethod
    def public_message(cls, output: str) -> str:
        for line in reversed(output.splitlines()):
            if line.startswith(cls.prefix):
                return line
        return "Installer failed without a declared refusal; private traceback retained on host"


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
    holder = subprocess.Popen([sys.executable, "-c", HOLD_TRANSCRIPTS, path, str(held)],
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


@dataclass
class StoreBackup(ResourceBackup):
    """Snapshot declared databases through SQLite; restore only the selected files."""
    root: Path
    route: Path
    path: Path
    kept: ClassVar[int] = 2

    def files(self) -> dict[str, str]:
        return FieldCodec.decode(dict[str, str],
                                 FieldCodec.parse((self.path / 'files.json').read_text()))

    def retire(self) -> Path:
        archived = self.path.with_name(f'{self.path.name}.restored-{time.time_ns()}')
        self.path.rename(archived)
        directory = os.open(archived.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        print(f'Restored data backup kept at {archived}', flush=True)
        return archived

    def restore_route(self) -> None:
        if (self.path / 'active-route.json').exists():
            shutil.copy2(self.path / 'active-route.json', self.route)

    @classmethod
    def take(cls, root: Path, route: Path, files: tuple[Path, ...], spill_dir: Path) -> ResourceBackup:
        if not files:
            return NoBackup()
        path = spill_dir / BackupName.now(root).value
        backup = cls(root, route, path)
        selected = tuple(str(file.relative_to(root)) for file in files)
        if path.exists():
            if tuple(json.loads((path / 'selected.json').read_text())) != selected:
                raise UpgradeRefused('Existing backup belongs to different resources')
            backup.files()  # An interrupted snapshot is never treated as complete.
            return backup
        path.mkdir(mode=0o700, parents=True)
        _atomic_write_text(path / 'selected.json', json.dumps(selected), fsync_parent=True)
        contents = backup.capture(files)
        if route.exists():
            shutil.copy2(route, path / 'active-route.json')
        _atomic_write_text(path / 'files.json', json.dumps(FieldCodec.encode(contents), indent=1),
                           fsync_parent=True)
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
        return (CoordinationStore.digest(path) if CoordinationStore.is_database(path)
                else hashlib.sha256(path.read_bytes()).hexdigest())

    @staticmethod
    def copy_file(source: str | Path, target: str | Path) -> Path:
        """Copy one store through its SQLite snapshot, never through live WAL bytes."""
        source, target = Path(source), Path(target)
        if CoordinationStore.is_database(source):
            with closing(CoordinationStore.read_snapshot(source)) as reader:
                with closing(sqlite3.connect(target)) as writer:
                    reader.backup(writer)
            shutil.copystat(source, target)
        else:
            shutil.copy2(source, target)
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
                elif (
                    path.is_file()
                    and not path.is_symlink()
                    and CoordinationStore.is_database(path)
                ):
                    omitted.update(
                        companion.name for companion in CoordinationStore.files(path)[1:]
                    )
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
            directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return contents

    def original(self) -> bool:
        if not (self.path / 'files.json').exists():
            return False
        saved = self.files()
        for name in json.loads((self.path / 'selected.json').read_text()):
            target = self.root / name
            if name not in saved:
                if target.exists():
                    return False
            elif not target.exists() or self.digest(target) != saved[name]:
                return False
        return True

    def restore(self) -> None:
        saved = self.files()
        for name in json.loads((self.path / 'selected.json').read_text()):
            target = self.root / name
            if name in saved:
                descriptor, temporary = tempfile.mkstemp(prefix='.restore-', dir=target.parent)
                os.close(descriptor)
                stage = Path(temporary)
                try:
                    shutil.copy2(self.data_root / name, stage)
                    with stage.open('rb') as ready:
                        os.fsync(ready.fileno())
                    os.replace(stage, target)
                finally:
                    stage.unlink(missing_ok=True)
            else:
                target.unlink(missing_ok=True)
            for companion in CoordinationStore.files(target)[1:]:
                companion.unlink(missing_ok=True)
        directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        if not self.original():
            raise UpgradeRefused('Selected stores differ after backup restoration')
        self.restore_route()


class NoBackup(ResourceBackup):
    """A runtime-only install owns no files, paths or snapshot operations."""

    def retain(self, spill_dir: Path) -> tuple[Path, ...]:
        return ()

    def original(self) -> bool:
        return True

    def restore(self) -> None:
        pass

    def report(self) -> BackupReport:
        return BackupReport(self.description, 0)

    @property
    def description(self) -> str:
        return 'none'
