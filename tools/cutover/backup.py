"""The update owner's snapshots: whole buses or declared store files."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Collection
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Iterator, Self

from agent_comms.coordination_database import CoordinationStore
from agent_comms.external_transcript import ExternalTranscript
from agent_comms.locked_store import LockedStore, RuntimeFile
from agent_comms.store_files import _atomic_write_text

if TYPE_CHECKING:
    from upgrade_bus import Stack


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


class UpgradeRefused(Exception):
    """The update stopped without permission to discard its original data."""
    prefix: ClassVar[str] = "upgrade-bus: refused:"

    @property
    def output(self) -> str:
        return f"{self.prefix} {self}"

    @classmethod
    def public_message(cls, output: str) -> str:
        for line in reversed(output.splitlines()):
            if line.startswith(cls.prefix):
                return line
        return "Installer failed without a declared refusal; private traceback retained on host"


def bus_files(root: Path) -> dict[str, str]:
    """Hash saved data once per SQLite store, ignoring runtime files and locks."""
    paths = [path for path in sorted(set(root.rglob("*")) - RuntimeFile.paths_in(root))
             if path.is_file() and not path.is_symlink() and not path.name.endswith(".lock")]
    stores = {path for path in paths if CoordinationStore.is_database(path)}
    companions = {companion for store in stores for companion in CoordinationStore.files(store)[1:]}
    return {str(path.relative_to(root)): CoordinationStore.digest(path) if path in stores
            else hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths if path not in companions}


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


@dataclass
class Backup:
    root: Path
    route: Path
    path: Path

    @classmethod
    def take(cls, root: Path, route: Path, files: tuple[Path, ...], stack: Stack) -> Self:
        path = stack.backup_root / f'{root.name}.upgrade-{time.time_ns()}'
        backup = cls(root, route, path).capture(files)
        backup.prune(stack.backup_limit)
        return backup

    def prune(self, limit: int) -> None:
        """Keep this rollback archive and the newest other completed archives."""
        completed = sorted(
            (path for path in self.path.parent.iterdir()
             if path.is_dir() and not path.is_symlink() and (path / 'files.json').is_file()),
            key=lambda path: (path / 'files.json').stat().st_mtime_ns,
            reverse=True,
        )
        others = [path for path in completed if path != self.path]
        removed = others[limit - 1:]
        for path in removed:
            shutil.rmtree(path)
        if removed:
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            print(f'Pruned {len(removed)} completed install backups', flush=True)

    def capture(self, files: tuple[Path, ...]) -> Self:
        """One shared completion protocol for whole-bus and selected-store snapshots."""
        selected = tuple(str(file.relative_to(self.root)) for file in files)
        if self.path.exists():
            if tuple(json.loads((self.path / 'selected.json').read_text())) != selected:
                raise UpgradeRefused('Existing backup belongs to different resources')
            self.files()  # An interrupted snapshot is never treated as complete.
            return self
        self.path.mkdir(mode=0o700, parents=True)
        _atomic_write_text(self.path / 'selected.json', json.dumps(selected), fsync_parent=True)
        contents = self.capture_files(files)
        if self.route.exists():
            shutil.copy2(self.route, self.path / 'active-route.json')
        _atomic_write_text(self.path / 'files.json', json.dumps(contents, indent=1), fsync_parent=True)
        return self

    def capture_files(self, files: tuple[Path, ...]) -> dict[str, str]:
        with tarfile.open(self.path / 'bus.tar', 'w') as archive:
            for path in files:
                archive.add(path, str(Path(self.root.name) / path.relative_to(self.root)))
        with (self.path / 'bus.tar').open('rb') as ready:
            os.fsync(ready.fileno())
        return bus_files(self.root)

    def files(self) -> dict[str, str]:
        return json.loads((self.path / 'files.json').read_text())

    def original(self) -> bool:
        return self.files() == bus_files(self.root)

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

    def restore(self) -> None:
        """Extract beside the bus, then swap; failed extraction keeps the bus intact."""
        staged = Path(tempfile.mkdtemp(dir=self.root.parent, prefix=f'.{self.root.name}.restore.'))
        with tarfile.open(self.path / 'bus.tar') as archive:
            archive.extractall(staged, filter='tar')
        failed = self.root.with_name(f'.{self.root.name}.failed-{self.path.name}')
        with transcripts_held(staged / self.root.name):
            os.rename(self.root, failed)
            os.rename(staged / self.root.name, self.root)
            staged.rmdir()
            if not self.original():
                raise UpgradeRefused(f'The restored bus differs; failed data is at {failed}')
        self.restore_route()
        print(f'Restored the bus; failed data is kept at {failed}', file=sys.stderr)


class StoreBackup(Backup):
    """Snapshot declared databases through SQLite; restore only the selected files."""

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

    def capture_files(self, files: tuple[Path, ...]) -> dict[str, str]:
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
