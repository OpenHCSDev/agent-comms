"""Owner-installed default route for a supervised private-root migration.

An absent route retains the historical default. A present but invalid route
fails closed; it never sends a message to a guessed root.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from abc import ABC
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar

from .bus import Bus
from .child_process import Platform
from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import FieldCodec, MalformedRecord, PathText
from .launch_root import LaunchRoot
from .private_nk_entrypoint import PrivateNkLaunch, RootLaunch
from .private_path import ObservedPath, OwnedDirectoryRole, PrivateDirectoryRole, PrivateFileRole
from .recorded_bus import RecordedBusIdentity
from .store_files import DependentStateTier, SingleFileStore, StoreLockContention

if TYPE_CHECKING:
    from .installer.reporting import RootWriter

# Publishers exclude only each other: a second one waits out the first install's route step.
ROUTE_PUBLICATION_WAIT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class ActiveRouteFile(SingleFileStore, DependentStateTier):
    """One route publication; competing installers spend a fresh bounded wait."""

    path: Path

    @classmethod
    def contention(cls) -> StoreLockContention:
        return StoreLockContention(ROUTE_PUBLICATION_WAIT_SECONDS)


@dataclass(frozen=True, slots=True)
class CommsRoute(ABC):
    """A resolved selection, not a service or permission to mutate its root."""

    root: Path

    @classmethod
    def current(cls, path: Path | None = None) -> CommsRoute:
        """The published route, independent of any client root override."""
        return read_active_route(path) or LocalRoute.default()

    def observe_root(self) -> Path:
        return self.root.resolve()

    def launch(self) -> RootLaunch:
        """This root's launch, decoded from its marker and this installation's pin."""
        return RootLaunch.derive(self)



@dataclass(frozen=True, slots=True)
class LocalRoute(CommsRoute):
    """Explicit/environment roots and the unconfigured historical default."""

    @classmethod
    def default(cls) -> LocalRoute:
        return cls(Path.home() / ".agent-comms")



class AbsoluteRoutePathText(PathText):
    """Route roots and handed-down native packages share one absolute-path boundary."""

    @classmethod
    def require_absolute(cls, path: Path) -> Path:
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("active comms route requires absolute root and package identities")
        return path

    @classmethod
    def from_text(cls, value: str) -> Path:
        return cls.require_absolute(super().from_text(value))

    @classmethod
    def encode(cls, value: object) -> str:
        text = super().encode(value)
        cls.require_absolute(value)
        return text


@dataclass(frozen=True, slots=True)
class ActiveRoute(CommsRoute):
    """The default private root; each installation launches its own pinned native package."""
    root: Annotated[Path, AbsoluteRoutePathText]
    bus_identity: RecordedBusIdentity
    # The file format: the writer stamps it and the reader refuses any other before decoding.
    version: ClassVar[int] = 3
    # The CLI command that replaces a file this reader refuses; it registers under this name.
    repair_command: ClassVar[str] = "replace-route"

    @classmethod
    def published_launch(cls) -> LaunchRoot:
        match read_active_route():
            case ActiveRoute() as route:
                if not route.root.is_dir():
                    raise ValueError("active comms route root is missing")
                route.observe_root()
                return LaunchRoot.from_path(route.root)
            case _:
                raise RelationViolationError(
                    "No active Comms route is published; --comms-root or --root is required."
                )

    def to_record(self) -> dict[str, object]:
        return {"version": self.version, **FieldCodec.encode(self)}

    def encoded(self) -> bytes:
        """The route file's bytes for this route."""
        return (json.dumps(self.to_record(), sort_keys=True) + "\n").encode()

    def rebound(self) -> ActiveRoute:
        """This route naming the bus its root holds now. A restore that copies the bus
        back gives it a new file identity, so the restored route names that one."""
        return replace(self, bus_identity=RecordedBusIdentity.capture(Bus.path_in(self.root)))

    @classmethod
    def from_record(cls, value: object, path: Path) -> ActiveRoute:
        value = FieldCodec.decode(dict[str, object], value)
        match value:
            case {"version": int(found), **record} if found == cls.version:
                pass
            case _:
                stamped = value.get("version")
                raise MalformedRecord(
                    f"active comms route {path} is version {stamped!r}; "
                    f"this installation reads version {cls.version}. "
                    "Replace it with the bus it should select: "
                    f"agent-comms --root ROOT {cls.repair_command}")
        try:
            route = FieldCodec.decode(cls, record)
        except MalformedRecord as error:
            raise MalformedRecord(f"active comms route {path} is invalid: {error}") from error
        return route

    def selects_its_root(self) -> bool:
        """A route selects the database observed when it was published."""
        return self.root.is_dir() and self.bus_identity.matches(
            ObservedPath.at(Bus.path_in(self.root)))

    def observe_root(self) -> Path:
        if not self.selects_its_root():
            raise RelationViolationError("route bus was replaced")
        return super(ActiveRoute, self).observe_root()

    def launch(self) -> PrivateNkLaunch:
        return RootLaunch.derive(self).require_private()


def resolve_comms_route(root: Path | str | None = None) -> CommsRoute:
    """Resolve one current selection without creating stores or reading registry."""
    if root is not None:
        return LocalRoute(Path(root).expanduser().absolute())
    return LocalRoute(LaunchRoot.current().path)


class RoutePublicationUnknownError(RelationViolationError):
    """The new route may be visible after a failed durability boundary."""


def active_route_path() -> Path:
    return Path.home() / ".local/state/agent-comms" / RouteFile.filename


class RouteFileNotPrivate(ValueError):
    def __init__(self) -> None:
        super().__init__("active comms route must be one owner-only regular file")


def read_active_route(path: Path | None = None) -> ActiveRoute | None:
    """Decode the one trusted default route; explicit roots bypass this file."""
    path = active_route_path() if path is None else path
    refusal = RouteFileNotPrivate()
    try:
        fd = PrivateFileRole.read(path, refusal)
    except FileNotFoundError:
        return None
    try:
        PrivateDirectoryRole.require(
            path.parent.lstat(), ValueError("active comms route directory must be owner-only"))
        info = os.fstat(fd)
        if not 0 < info.st_size <= 4096:
            raise refusal
        raw = os.read(fd, 4097)
    finally:
        os.close(fd)
    try:
        current = path.lstat()
    except FileNotFoundError as error:
        raise ValueError("active comms route changed while reading") from error
    if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
    ):
        raise ValueError("active comms route changed while reading")
    try:
        value = FieldCodec.parse(raw)
    except MalformedRecord as error:
        raise MalformedRecord("active comms route is invalid JSON") from error
    return ActiveRoute.from_record(value, path)


def route_new_bus(route: ActiveRoute, path: Path) -> str:
    """Route to this bus unless the file already selects one: install's answer and the
    owner's repair. A replaced file is kept beside the new one under a backup name, so a
    live route is still only ever installed once."""
    with _route_directory(route, path) as (path, directory):
        return RouteFile.read(path).route_new_bus(route, path, directory)


class RouteFileSelection(DeclaredFamily, affix="Selection", declared_name=None):
    """The outgoing reader's validated choice owns whether this bus may be published."""

    def publish(self, route: ActiveRoute, path: Path, directory: int) -> str:
        return RouteFile.read(path).route_new_bus(route, path, directory)


@dataclass(frozen=True)
class NoRouteSelection(RouteFileSelection):
    """No route selects an existing bus; the installer may publish this one."""


@dataclass(frozen=True)
class PublishedRouteSelection(RouteFileSelection):
    root: Annotated[Path, PathText]

    def publish(self, route: ActiveRoute, path: Path, directory: int) -> str:
        if self.root.resolve() != route.root.resolve():
            return f"kept {path}: it selects {self.root}"
        return super().publish(route, path, directory)


class RouteFile(ABC):
    """What the default route file holds when install or the owner's repair reads it. A file
    that selects no bus (unreadable, an old version, or naming a root gone or replaced) is
    replaced by both; every other state owns its own answer."""

    filename: ClassVar[str] = "active-route.json"

    @staticmethod
    def read(path: Path) -> RouteFile:
        try:
            current = read_active_route(path)
        except MalformedRecord:
            return ReplaceableRoute()
        match current:
            case ActiveRoute() as selected if selected.selects_its_root():
                return SelectingRoute(selected)
            case ActiveRoute():
                return ReplaceableRoute()
        return AbsentRoute()

    def selection(self) -> RouteFileSelection:
        return NoRouteSelection()

    def launch_root(self) -> Path:
        """Where install runs: the default root, where it creates a bus."""
        return LocalRoute.default().root

    def replace(self, route: ActiveRoute, path: Path, directory: int) -> Path:
        name = Path(self.filename)
        backup = path.with_name(
            f"{name.stem}.{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex}.bak{name.suffix}")
        _publish_active_route_locked(route, path, directory, backup=backup)
        return backup

    def route_new_bus(self, route: ActiveRoute, path: Path, directory: int) -> str:
        return f"replaced {path}; old route kept at {self.replace(route, path, directory)}"

    def outgoing_writer(self, outgoing: RootWriter, unposted: RootWriter) -> RootWriter:
        """Who posts install reports before install routes to its bus: the outgoing runtime,
        which borrows this file like every client."""
        return outgoing


class ReplaceableRoute(RouteFile):
    """Selects no bus: install and the repair both replace it."""

    def outgoing_writer(self, outgoing: RootWriter, unposted: RootWriter) -> RootWriter:
        """Every borrower of the default root refuses this file, the outgoing runtime
        included, so nothing posts until install's route step replaces it."""
        return unposted


class AbsentRoute(RouteFile):
    def route_new_bus(self, route: ActiveRoute, path: Path, directory: int) -> str:
        _publish_active_route_locked(route, path, directory)
        return f"published {path}"


@dataclass(frozen=True)
class SelectingRoute(RouteFile):
    """Still selects its own bus: it is kept, and install runs there."""

    current: ActiveRoute

    def selection(self) -> RouteFileSelection:
        return PublishedRouteSelection(self.current.observe_root())

    def launch_root(self) -> Path:
        return self.current.root

    def route_new_bus(self, route: ActiveRoute, path: Path, directory: int) -> str:
        return f"kept {path}: it selects {self.current.root}"


@contextmanager
def _route_directory(route: ActiveRoute, path: Path | None) -> Iterator[tuple[Path, int]]:
    """The checked route path and its directory FD, held with route EX."""
    path = active_route_path() if path is None else path
    if path.name != RouteFile.filename or not path.is_absolute():
        raise ValueError("active comms route requires its absolute route path")
    FieldCodec.encode(route)
    route.launch().validate()
    with route_directory(path) as directory:
        yield path, directory


@contextmanager
def route_directory(path: Path) -> Iterator[int]:
    """One publisher's route snapshot or mutation. Only publishers take the route file's
    lock; clients never do, so a reader cannot hold an install. A write that a published
    route has left behind is refused by its store, under that store's write lock."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    refusal = ValueError("active comms route directory changed or is not owned")
    with ActiveRouteFile(path).guarded_write():
        directory = OwnedDirectoryRole.read(path.parent, refusal)
        try:
            OwnedDirectoryRole.held(directory, path.parent, refusal)
            Platform.current().make_private_descriptor(directory, 0o700)
            yield directory
        finally:
            os.close(directory)


def _publish_active_route_locked(
    route: ActiveRoute,
    path: Path,
    directory: int,
    *,
    backup: Path | None = None,
) -> None:
    """Publish through a checked directory FD already held with route EX: link a first route,
    or move an unreadable one to ``backup`` and replace it."""
    temporary: str | None = None
    try:
        refusal = ValueError("active comms route directory changed or is not owned")
        OwnedDirectoryRole.held(directory, path.parent, refusal)
        info = os.fstat(directory)
        Platform.current().make_private_descriptor(directory, 0o700)
        if backup is None and read_active_route(path) is not None:
            raise ValueError("active comms route is already installed")
        payload = route.encoded()
        temporary = f".active-route-{uuid.uuid4().hex}.tmp"
        fd = PrivateFileRole.create(
            temporary, RouteFileNotPrivate(),
            dir_fd=directory)
        try:
            view = memoryview(payload)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        route.launch().validate()
        parent = path.parent.lstat()
        if (parent.st_dev, parent.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("active comms route directory changed before publication")
        if backup is None:
            try:
                os.link(
                    temporary,
                    path.name,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
            except FileExistsError as error:
                raise ValueError("active comms route is already installed") from error
            try:
                os.unlink(temporary, dir_fd=directory)
                temporary = None
                os.fsync(directory)
            except OSError as error:
                raise RoutePublicationUnknownError(
                    "active comms route publication outcome UNKNOWN after link"
                ) from error
        else:
            os.link(path.name, backup.name, src_dir_fd=directory, dst_dir_fd=directory,
                    follow_symlinks=False)
            try:
                os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
                temporary = None
                os.fsync(directory)
            except OSError as error:
                raise RoutePublicationUnknownError(
                    "active comms route publication outcome UNKNOWN after replacement"
                ) from error
    finally:
        if temporary is not None:
            os.unlink(temporary, dir_fd=directory)
