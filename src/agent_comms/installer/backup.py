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
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache, partial
from pathlib import Path
from string import Formatter
from typing import Annotated, ClassVar, Iterator, Self

from agent_comms.active_route import (
    ActiveRoute,
    ActiveRouteFile,
    RouteFile,
    RouteFileSelection,
    active_route_path,
)
from agent_comms.child_process import Platform
from agent_comms.nominal.field_codec import FieldCodec, MalformedRecord, PathText
from agent_comms.installer.refusals import UpgradeRefused
from agent_comms.locked_store import (
    DeclaredFile,
    FilesRequest,
    LiveFilesRequest,
    LockedStore,
    RootFile,
    RollbackRetained,
    RuntimeFile,
    UndeclaredFile,
)
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceOperator, MaintenanceState
from agent_comms.private_path import FileObservation, FileRevision, MissingFile, PrivateDirectoryRole
from agent_comms.registry_store import RegistryStore
from agent_comms.store_files import AtomicFile, DirectoryAtomicFile
from agent_comms.typed_table import ExternalStore


@dataclass(frozen=True)
class SavedFile:
    size: int
    mtime_ns: int
    inode: int

    @classmethod
    def at(cls, path: Path) -> Self:
        observed = path.stat()
        return cls(observed.st_size, observed.st_mtime_ns, observed.st_ino)


class SavedRoute(ABC):
    """The original route's prepared restoration, including original absence."""

    @staticmethod
    def read(path: Path) -> SavedRoute:
        source = ActiveRouteFile(path).capture()

        def present(revision: FileRevision) -> SavedRoute:
            info = path.stat()
            content = source.content
            try:
                route = ActiveRoute.from_bytes(content, path)
            except MalformedRecord:
                pass  # An unreadable original is restored byte-for-byte.
            else:
                content = route.rebound().encoded()
            return PresentSavedRoute(content, info.st_mode, info.st_nlink)

        return source.revision.either(present, AbsentSavedRoute)

    @abstractmethod
    def matches(self, path: Path) -> bool: ...

    @abstractmethod
    def restore(self, path: Path, directory: int) -> None: ...


class AbsentSavedRoute(SavedRoute):
    def matches(self, path: Path) -> bool:
        return not path.exists()

    def restore(self, path: Path, directory: int) -> None:
        os.unlink(path.name, dir_fd=directory)
        os.fsync(directory)


@dataclass(frozen=True)
class PresentSavedRoute(SavedRoute):
    content: bytes
    mode: int
    links: int

    def matches(self, path: Path) -> bool:
        try:
            current = path.stat()
        except FileNotFoundError:
            return False
        return (current.st_mode == self.mode and current.st_nlink == self.links
                and path.read_bytes() == self.content)

    def restore(self, path: Path, directory: int) -> None:
        DirectoryAtomicFile(path, mode=stat.S_IMODE(self.mode), directory=directory).write_bytes(self.content)


def bus_paths(files: FilesRequest, external: tuple[type[ExternalStore], ...]) -> tuple[Path, ...]:
    """One inventory of bus content and the selected external stores."""
    root = files.root
    excluded = RuntimeFile.paths_in(root)
    observed: list[Path] = []
    for path in root.rglob("*"):
        if path in excluded:
            continue
        info = path.lstat()
        if stat.S_ISREG(info.st_mode):
            observed.append(path)
    return (*observed, *(files.location(resource) for store in external
                         for resource in store.resources(files)))


def settle_bus_files(files: FilesRequest, external: tuple[type[ExternalStore], ...] = ()) -> None:
    """Each selected declaration owns whether settling is valid for its file."""
    paths = bus_paths(files, external)
    for resource in RootFile.owned_files(files, paths):
        resource.owner.settle(files.location(resource))


def bus_files(files: FilesRequest, external: tuple[type[ExternalStore], ...] = ()
              ) -> dict[DeclaredFile | UndeclaredFile, dict[Path, SavedFile]]:
    """One declaration-derived inventory for bus and writer-owned resources."""
    paths = bus_paths(files, external)
    saved = {}
    for resource in RootFile.owned_files(files, paths):
        source = files.location(resource)
        observed = {member: member.stat() for member in resource.owner.files(source) if member.is_file()}
        saved[resource] = {
            resource.owner.relocated(source, resource.path, member): SavedFile.at(member)
            for member in resource.owner.snapshot_members(source, observed) if member in observed
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


@dataclass
class StoreBackup:
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
    def complete_interrupted(
        cls, root: Path, spill_dir: Path,
        rebuild: Callable[[MaintenanceState], MaintenanceState],
    ) -> tuple[Path, ...]:
        """Finish every restore a killed install left unfinished, before anything reads
        the bus: take its abandoned maintenance hold, restore, release, then retire the
        backup so the next install never meets it again."""
        retired = []
        for backup in cls.interrupted(root, spill_dir):
            maintenance = MaintenanceBarrier(RegistryStore.at(root).path)
            hold = maintenance.begin(MaintenanceOperator.current())
            with hold.holding():
                hold = backup.restore(partial(rebuild, hold))
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
        def restore(saved: SavedRoute, directory: int) -> None:
            if FileRevision.capture(self.route) != self.route_revision:
                print(f'Keeping concurrently changed route: {self.route}', file=sys.stderr)
                return
            if saved.matches(self.route):
                return
            saved.restore(self.route, directory)
            self.route_revision = FileRevision.capture(self.route)
            if not saved.matches(self.route):
                raise UpgradeRefused('Active route differs after backup restoration')

        ActiveRouteFile(self.route).update(
            partial(SavedRoute.read, self.path / RouteFile.filename), restore)

    def require_route_revision(self) -> None:
        if FileRevision.capture(self.route) != self.route_revision:
            raise UpgradeRefused('Active route changed after its backup')

    def rebind_route(self, route: ActiveRoute, selected: RouteFileSelection) -> str:
        def publish(current: RouteFile, prepared: Callable[[], None], directory: int) -> str:
            self.require_route_revision()
            try:
                return selected.publish(current, route, self.route, prepared, directory)
            finally:
                # The store still owns EX, including uncertain durability acknowledgement.
                self.route_revision = FileRevision.capture(self.route)

        return ActiveRouteFile(self.route).publish(route, publish)

    def route_original(self) -> bool:
        return SavedRoute.read(self.path / RouteFile.filename).matches(self.route)

    @classmethod
    def take(cls, root: Path, route: Path, files: tuple[Path, ...], spill_dir: Path) -> StoreBackup:
        files = tuple(dict.fromkeys((*files, *RollbackRetained.retained_in(root))))
        path = spill_dir / BackupName.now(root).value
        backup = cls(root, route, path)
        resources = RootFile.owned_files(LiveFilesRequest(root), files)
        selected = tuple(sorted(resource.path for resource in resources))
        if path.exists():
            if backup.selected_files() != selected:
                raise UpgradeRefused('Existing backup belongs to different resources')
            backup.files()  # An interrupted snapshot is never treated as complete.
            def confirm(saved: SavedRoute, directory: int) -> None:
                if not saved.matches(route):
                    raise UpgradeRefused('Existing backup belongs to a different route')
                backup.route_revision = FileRevision.capture(route)
            ActiveRouteFile(route).update(
                partial(SavedRoute.read, path / RouteFile.filename), confirm)
            return backup
        PrivateDirectoryRole.make_parents(path.parent)
        path.mkdir(mode=PrivateDirectoryRole.permissions)
        AtomicFile(path / 'selected.json').write_text(json.dumps(FieldCodec.encode(
            selected, tuple[Annotated[Path, PathText], ...])))
        contents = backup.capture(resources)
        def capture_route(_: None, directory: int) -> None:
            if route.exists():
                shutil.copy2(route, path / RouteFile.filename)
            backup.route_revision = FileRevision.capture(route)
        ActiveRouteFile(route).update(lambda: None, capture_route)
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
        owners = {resource.live(source): resource.owner
                  for resource in RootFile.owned_files(LiveFilesRequest(source), paths)}
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

    def capture(self, resources: Collection[DeclaredFile | UndeclaredFile]) -> dict[Path, str]:
        return {name: digest for resource in resources
                for name, digest in resource.owner.capture_file(
                    resource.live(self.root), resource.path, self.data_root).items()}

    def original(self) -> bool:
        return self.route_original() and self.stores_original()

    def stores_original(self) -> bool:
        if not (self.path / 'files.json').exists():
            return False
        saved = self.files()
        return all(resource.owner.file_matches(resource.live(self.root), resource.path, saved)
                   for resource in RootFile.named_files(
                       LiveFilesRequest(self.root), self.selected_files()))

    def restore[Restored](self, rebuild: Callable[[], Restored]) -> Restored:
        """Restore selected bytes, then rebuild omitted projections before marking complete."""
        saved = self.files()
        selected = {resource.path: resource for resource in RootFile.named_files(
            LiveFilesRequest(self.root), self.selected_files())}
        registry = tuple(path.relative_to(self.root) for path in RegistryStore.at(self.root).written)
        # The registry owns this ordering: restore its snapshot before publishing
        # the matching guard, after every unrelated selected file.
        ordered = (
            *(name for name in sorted(selected) if name not in registry),
            *(name for name in registry if name in selected),
        )
        AtomicFile(self.restoring).write_text('')
        for name in ordered:
            resource = selected[name]
            source = resource.live(self.root)
            if not resource.owner.file_matches(source, name, saved):
                resource.owner.restore_file(source, name, self.data_root, saved)
        Platform.current().sync_directory(self.root)
        self.restore_route()
        if not self.stores_original():
            raise UpgradeRefused('Selected stores differ after backup restoration')
        restored = rebuild()
        self.restoring.unlink()
        Platform.current().sync_directory(self.path)
        return restored
