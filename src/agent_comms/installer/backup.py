"""The update owner's snapshots: whole buses or declared store files."""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache
from pathlib import Path
from string import Formatter
from typing import Annotated, ClassVar, Iterator, Self

from agent_comms.active_route import (
    ActiveRoute,
    RouteFile,
    RouteFileSelection,
    _route_directory,
    active_route_path,
    read_active_route,
    route_directory,
)
from agent_comms.child_process import Platform
from agent_comms.field_codec import FieldCodec, MalformedRecord, PathText
from agent_comms.installer.files import InstallationFile, InstallationFiles
from agent_comms.installer.refusals import UpgradeRefused
from agent_comms.locked_store import (
    DeclaredFile,
    LockedStore,
    RootFile,
    RuntimeFile,
    UndeclaredFile,
)
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceOperator
from agent_comms.private_path import FileObservation, FileRevision, MissingFile
from agent_comms.registry_store import RegistryStore
from agent_comms.store_files import AtomicFile


@dataclass(frozen=True)
class SavedFile:
    size: int
    mtime_ns: int
    inode: int

    @classmethod
    def at(cls, path: Path) -> Self:
        observed = path.stat()
        return cls(observed.st_size, observed.st_mtime_ns, observed.st_ino)


def observed_files(root: Path) -> dict[Path, os.stat_result]:
    """Every regular file an installation carries, with its metadata."""
    excluded = RuntimeFile.paths_in(root)
    observed: dict[Path, os.stat_result] = {}
    for path in root.rglob("*"):
        if path in excluded:
            continue
        info = path.lstat()
        if stat.S_ISREG(info.st_mode):
            observed[path] = info
    return observed


def settle_bus_files(root: Path, external: tuple[InstallationFile, ...] = ()) -> None:
    """Settle bus resources; external bytes must reach backup without a prior write."""
    external_paths = {path for file in external for path in file.owner.files(file.path)}
    for resource, owner in RootFile.owned_files(root, observed_files(root)).items():
        if root / resource.path not in external_paths:
            owner.settle(root / resource.path)


def bus_files(root: Path, external: tuple[InstallationFile, ...] = ()
              ) -> dict[DeclaredFile | UndeclaredFile, dict[Path, SavedFile]]:
    """Record declared identities and physical metadata at live or copied locations."""
    observed = observed_files(root)
    external_paths = {path for file in external for path in file.owner.files(file.path)}
    saved = {
        resource: {
            member.relative_to(root): SavedFile(
                observed[member].st_size, observed[member].st_mtime_ns, observed[member].st_ino,
            ) for member in owner.snapshot_members(root / resource.path, observed)
        } for resource, owner in RootFile.owned_files(root, observed).items()
        if root / resource.path not in external_paths
    }
    for file in external:
        members = {member: member.stat() for member in file.owner.files(file.path)
                   if member.is_file()}
        saved[DeclaredFile(file.owner, file.name)] = {
            file.member_name(member): SavedFile.at(member)
            for member in file.owner.snapshot_members(file.path, members)
            if member in members
        }
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

    @property
    def needed(self) -> int:
        return self.bytes + max(self.bytes // 10, 1024**3)

    @classmethod
    def measure(cls, rewritten_bytes: int, filesystem: Path) -> Self:
        free = shutil.disk_usage(filesystem).free
        return cls(rewritten_bytes, free)

    def require(self) -> None:
        if self.free < self.needed:
            raise UpgradeRefused(
                f"Restore needs {self.needed} bytes on the bus filesystem; "
                f"only {self.free} bytes are free")


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

    def undo(self) -> None:
        """Put back whatever changed since the snapshot; an untouched root stays as it is."""
        if not self.original():
            self.restore()

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

    def files(self) -> dict[Path, str]:
        stored = FieldCodec.decode(dict[str, str],
                                   FieldCodec.parse((self.path / 'files.json').read_text()))
        return {PathText.decode(path): digest for path, digest in stored.items()}

    def selected_files(self) -> tuple[Annotated[Path, PathText], ...]:
        return FieldCodec.decode(tuple[Annotated[Path, PathText], ...],
                                 FieldCodec.parse((self.path / 'selected.json').read_text()))

    @classmethod
    def interrupted(cls, root: Path, spill_dir: Path) -> tuple[StoreBackup, ...]:
        if not spill_dir.exists():
            return ()
        route = active_route_path()
        backups = (
            cls(root, route, spill_dir / name.value)
            for name in sorted(BackupName.matching(
                root, (path.name for path in spill_dir.iterdir() if path.is_dir())
            ), reverse=True)
        )
        return tuple(backup for backup in backups if backup.restoring.exists())

    @classmethod
    def complete_interrupted(cls, root: Path, spill_dir: Path) -> tuple[Path, ...]:
        """Finish every restore a killed install left unfinished, before anything reads
        the bus: take its abandoned maintenance hold, restore, release, then retire the
        backup so the next install never meets it again."""
        retired = []
        for backup in cls.interrupted(root, spill_dir):
            maintenance = MaintenanceBarrier(RegistryStore.at(root).path)
            hold = maintenance.begin(MaintenanceOperator.current())
            with hold.holding():
                backup.restore()
            maintenance.clear(hold)
            retired.append(backup.retire())
        return tuple(retired)

    @property
    def restoring(self) -> Path:
        """Present from a restore's first write until it verifies: the only record that a
        restore began and did not finish. A backup that differs from the live root was never
        necessarily restored; every completed install leaves one behind."""
        return self.path / 'restoring'

    def retire(self) -> Path:
        archived = self.path.with_name(f'{self.path.name}.restored-{time.time_ns()}')
        self.path.rename(archived)
        Platform.current().sync_directory(archived.parent)
        print(f'Restored data backup kept at {archived}', flush=True)
        return archived

    def restore_route(self) -> None:
        with route_directory(self.route) as directory:
            if FileRevision.capture(self.route) != self.route_revision:
                print(f'Keeping concurrently changed route: {self.route}', file=sys.stderr)
                return
            if self.route_original():
                return
            saved = self.path / RouteFile.filename
            if saved.exists():
                AtomicFile(self.route).write_bytes(self.restored_route())
            else:
                os.unlink(self.route.name, dir_fd=directory)
                os.fsync(directory)
            self.route_revision = FileRevision.capture(self.route)
            if not self.route_original():
                raise UpgradeRefused('Active route differs after backup restoration')

    def require_route_revision(self) -> None:
        if FileRevision.capture(self.route) != self.route_revision:
            raise UpgradeRefused('Active route changed after its backup')

    def rebind_route(self, route: ActiveRoute, selected: RouteFileSelection) -> str:
        with _route_directory(route, self.route) as (path, directory):
            self.require_route_revision()
            try:
                return selected.publish(route, path, directory)
            finally:
                # Observe our mutation while EX still excludes another publisher,
                # including a replacement whose durability acknowledgement failed.
                self.route_revision = FileRevision.capture(path)

    def restored_route(self) -> bytes:
        """The saved route, naming the bus its root holds now: restoring the stores copies
        the bus back under a new file identity, and the route names its bus by identity.
        An unreadable saved route selected no bus, and comes back as it was."""
        saved = self.path / RouteFile.filename
        try:
            route = read_active_route(saved)
        except MalformedRecord:
            return saved.read_bytes()
        return route.rebound().encoded()

    def route_original(self) -> bool:
        saved = self.path / RouteFile.filename
        if not saved.exists():
            return not self.route.exists()
        if not self.route.exists():
            return False
        current, original = self.route.stat(), saved.stat()
        return (current.st_mode == original.st_mode and current.st_nlink == original.st_nlink
                and self.route.read_bytes() == self.restored_route())

    @classmethod
    def take(cls, root: Path, route: Path, files: tuple[Path, ...], spill_dir: Path) -> StoreBackup:
        registry = RegistryStore.at(root).written
        if any(path in files for path in registry):
            files = tuple(dict.fromkeys((*files, *registry)))
        path = spill_dir / BackupName.now(root).value
        backup = cls(root, route, path)
        resources = InstallationFiles(root).selected(files)
        selected = tuple(file.name for file in resources)
        if path.exists():
            if backup.selected_files() != selected:
                raise UpgradeRefused('Existing backup belongs to different resources')
            backup.files()  # An interrupted snapshot is never treated as complete.
            with route_directory(route):
                if not backup.route_original():
                    raise UpgradeRefused('Existing backup belongs to a different route')
                backup.route_revision = FileRevision.capture(route)
            return backup
        path.mkdir(mode=0o700, parents=True)
        AtomicFile(path / 'selected.json').write_text(json.dumps(FieldCodec.encode(
            selected, tuple[Annotated[Path, PathText], ...])))
        contents = backup.capture(resources)
        with route_directory(route):
            if route.exists():
                shutil.copy2(route, path / RouteFile.filename)
            backup.route_revision = FileRevision.capture(route)
        AtomicFile(path / 'files.json').write_text(json.dumps(
            {PathText.encode(file): digest for file, digest in contents.items()}, indent=1))
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

    @classmethod
    def copy_tree(
        cls, source: Path, target: Path, *, excluded: Collection[Path] = ()
    ) -> None:
        """Copy committed files; their owners declare transient names before any stat."""

        paths = tuple(path for path in source.rglob("*")
                      if path.is_file() and not path.is_symlink())
        owners = {source / resource.path: owner
                  for resource, owner in RootFile.owned_files(source, paths).items()}
        omitted = set(paths) - owners.keys() | set(excluded)
        omitted.update(member for path, owner in owners.items()
                       for member in owner.files(path) if member != path)

        def passes(path: Path) -> bool:
            """Directories and links pass through; a file passes only with its owner, so one
            written after the owners were read, or a socket, is never copied."""
            listed = path in owners or stat.S_ISDIR(path.lstat().st_mode) or path.is_symlink()
            return listed and path not in omitted

        def transient(directory: str, names: list[str]) -> set[str]:
            return {path.name for path in (Path(directory) / name for name in names)
                    if LockedStore.is_staging(path) or not passes(path)}

        def copy_file(original: str, destination: str) -> Path:
            return owners[Path(original)].copy_file(original, destination)

        shutil.copytree(source, target, symlinks=True, ignore=transient, copy_function=copy_file)

    def capture(self, resources: tuple[InstallationFile, ...]) -> dict[Path, str]:
        return {name: digest for file in resources
                for name, digest in file.capture(self.data_root).items()}

    def original(self) -> bool:
        return self.route_original() and self.stores_original()

    def stores_original(self) -> bool:
        if not (self.path / 'files.json').exists():
            return False
        saved = self.files()
        return all(file.original(saved) for file in InstallationFiles(self.root).named(
            self.selected_files()))

    def restore(self) -> None:
        saved = self.files()
        selected = {file.name: file for file in InstallationFiles(self.root).named(
            self.selected_files())}
        registry = tuple(path.relative_to(self.root) for path in RegistryStore.at(self.root).written)
        # The registry owns this ordering: restore its snapshot before publishing
        # the matching guard, after every unrelated selected file.
        ordered = (
            *(name for name in selected if name not in registry),
            *(name for name in registry if name in selected),
        )
        AtomicFile(self.restoring).write_text('')
        for name in ordered:
            file = selected[name]
            if not file.original(saved):
                file.restore(self.data_root, saved)
        Platform.current().sync_directory(self.root)
        self.restore_route()
        if not self.stores_original():
            raise UpgradeRefused('Selected stores differ after backup restoration')
        self.restoring.unlink()
        Platform.current().sync_directory(self.path)
