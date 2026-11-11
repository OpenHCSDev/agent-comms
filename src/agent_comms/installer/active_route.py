"""Owner-installed default route for a supervised private-root migration.

An absent route retains the historical default. A present but invalid route
fails closed; it never sends a message to a guessed root.
"""

from __future__ import annotations

import json
import os
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar

from agent_comms.errors import RelationViolationError
from agent_comms.execution.child_process import Platform
from agent_comms.execution.launch_root import LaunchRoot
from agent_comms.execution.private_nk_entrypoint import PrivateNkLaunch, RootLaunch
from agent_comms.messaging.bus import Bus
from agent_comms.messaging.recorded_bus import RecordedBusIdentity
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import FieldCodec, MalformedRecord, PathText, ScratchName
from agent_comms.storage.locked_store import DocumentBytes
from agent_comms.storage.private_path import (
    FileRevision,
    MissingFile,
    ObservedPath,
    OwnedDirectoryRole,
    PrivateDirectoryRole,
    PrivateFileRole,
)
from agent_comms.storage.store_files import (
    DependentStateTier,
    DirectoryAtomicFile,
    ExclusiveDirectoryAtomicFile,
    FileTransactions,
    SingleFileStore,
    StoreLockContention,
)

if TYPE_CHECKING:
    from agent_comms.installer.reporting import RootWriter


@dataclass(frozen=True, slots=True)
class ActiveRouteFile(FileTransactions, SingleFileStore, DependentStateTier):
    """One route publication; competing installers spend a fresh bounded wait."""

    path: Path

    # Publishers exclude only each other while the first install publishes its route.
    contention = StoreLockContention(30.0)

    def capture(self) -> DocumentBytes:
        """Capture private route bytes; decoding belongs outside every publication guard."""
        path = self.path
        refusal = RouteFileNotPrivate()
        with ExitStack() as reading:
            # Only the open answers "no route published yet"; a missing directory below
            # is the changed-route failure this must still raise.
            try:
                fd = reading.enter_context(PrivateFileRole.reading(path, refusal))
            except FileNotFoundError:
                return DocumentBytes(MissingFile(), b"")
            PrivateDirectoryRole.require(
                path.parent.lstat(), ValueError("active comms route directory must be owner-only"))
            info = os.fstat(fd)
            if not 0 < info.st_size <= 4096:
                raise refusal
            raw = os.read(fd, 4097)
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
        return DocumentBytes(FileRevision.from_stat(info), raw)

    @contextmanager
    def _directory(self) -> Iterator[int]:
        """Pin the publication directory without lending any caller a store lock."""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        refusal = ValueError("active comms route directory changed or is not owned")
        with OwnedDirectoryRole.reading(self.path.parent, refusal) as directory:
            OwnedDirectoryRole.held(directory, self.path.parent, refusal)
            Platform.current().make_private_descriptor(directory, 0o700)
            yield directory

    def update[Value, Result](
        self, read: Callable[[], Value], change: Callable[[Value, int], Result],
    ) -> Result:
        """Read/prepare outside exclusion, then change the still-current route."""
        with self._directory() as directory:
            def commit(original: Value) -> Result:
                OwnedDirectoryRole.held(directory, self.path.parent,
                                        ValueError("active comms route directory changed"))
                return change(original, directory)
            return self.change_current(lambda: None, lambda _: nullcontext(read()), commit)

    def publish[Result](
        self, route: ActiveRoute,
        change: Callable[[RouteFile, Callable[[], None], int], Result],
    ) -> Result:
        """Stage and validate before EX; publication uses the current file's policy."""
        if self.path.name != RouteFile.filename or not self.path.is_absolute():
            raise ValueError("active comms route requires its absolute route path")
        payload = route.encoded()
        route.launch().validate()
        with self._directory() as directory:
            @contextmanager
            def prepare(_: None) -> Iterator[tuple[RouteFile, Callable[[], None]]]:
                original = RouteFile.read(self.path)
                with original.writer(self.path, directory).prepared(payload) as publish:
                    route.launch().validate()
                    yield original, publish

            def commit(prepared: tuple[RouteFile, Callable[[], None]]) -> Result:
                original, publish = prepared
                OwnedDirectoryRole.held(directory, self.path.parent,
                                        ValueError("active comms route directory changed"))
                route.observe_root()
                return change(original, publish, directory)

            return self.change_current(lambda: None, prepare, commit)


@dataclass(frozen=True, slots=True)
class CommsRoute(ABC):
    """A resolved selection, not a service or permission to mutate its root."""

    root: Path

    @classmethod
    def current(cls, path: Path | None = None) -> CommsRoute:
        """The published route, independent of any client root override."""
        return read_active_route(path).or_default()

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


class PublishedRoute(ABC):
    """What the default route file publishes: a route, or no route at all. Each consumer's
    question has its answer on the member, so no caller decides what absence means."""

    __slots__ = ()

    @abstractmethod
    def or_default(self) -> CommsRoute: ...

    @abstractmethod
    def published_launch_root(self) -> LaunchRoot: ...

    @abstractmethod
    def route_file(self) -> RouteFile: ...


class NoActiveRoute(PublishedRoute):
    """No default route file is published."""

    __slots__ = ()

    def or_default(self) -> CommsRoute:
        return LocalRoute.default()

    def published_launch_root(self) -> LaunchRoot:
        raise RelationViolationError(
            "No active Comms route is published; --comms-root or --root is required."
        )

    def route_file(self) -> RouteFile:
        return AbsentRoute()


@dataclass(frozen=True, slots=True)
class ActiveRoute(CommsRoute, PublishedRoute):
    """The default private root; each installation launches its own pinned native package."""
    root: Annotated[Path, AbsoluteRoutePathText]
    bus_identity: RecordedBusIdentity
    # The file format: the writer stamps it and the reader refuses any other before decoding.
    version: ClassVar[int] = 3
    # The CLI command that replaces a file this reader refuses; it registers under this name.
    repair_command: ClassVar[str] = "replace-route"

    @classmethod
    def published_launch(cls) -> LaunchRoot:
        return read_active_route().published_launch_root()

    def or_default(self) -> CommsRoute:
        return self

    def published_launch_root(self) -> LaunchRoot:
        if not self.root.is_dir():
            raise ValueError("active comms route root is missing")
        self.observe_root()
        return LaunchRoot.from_path(self.root)

    def route_file(self) -> RouteFile:
        return SelectingRoute(self) if self.selects_its_root() else ReplaceableRoute()

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
    def from_bytes(cls, raw: bytes, path: Path) -> ActiveRoute:
        try:
            value = FieldCodec.parse(raw)
        except MalformedRecord as error:
            raise MalformedRecord("active comms route is invalid JSON") from error
        return cls.from_record(value, path)

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


def active_route_path() -> Path:
    return Path.home() / ".local/state/agent-comms" / RouteFile.filename


class RouteFileNotPrivate(ValueError):
    def __init__(self) -> None:
        super().__init__("active comms route must be one owner-only regular file")


def read_active_route(path: Path | None = None) -> PublishedRoute:
    """Decode the one trusted default route; explicit roots bypass this file."""
    path = active_route_path() if path is None else path
    source = ActiveRouteFile(path).capture()
    return source.revision.either(
        lambda present: ActiveRoute.from_bytes(source.content, path), NoActiveRoute)


def route_new_bus(route: ActiveRoute, path: Path) -> str:
    """Route to this bus unless the file already selects one: install's answer and the
    owner's repair. A replaced file is kept beside the new one under a backup name, so a
    live route is still only ever installed once."""
    return ActiveRouteFile(path).publish(
        route, lambda current, publish, directory:
        current.route_new_bus(route, path, publish, directory))


class RouteFileSelection(DeclaredFamily, affix="Selection", declared_name=None):
    """The outgoing reader's validated choice owns whether this bus may be published."""

    def publish(self, current: RouteFile, route: ActiveRoute, path: Path,
                publish: Callable[[], None], directory: int) -> str:
        return current.route_new_bus(route, path, publish, directory)


@dataclass(frozen=True)
class NoRouteSelection(RouteFileSelection):
    """No route selects an existing bus; the installer may publish this one."""


@dataclass(frozen=True)
class PublishedRouteSelection(RouteFileSelection):
    root: Annotated[Path, PathText]

    def publish(self, current: RouteFile, route: ActiveRoute, path: Path,
                publish: Callable[[], None], directory: int) -> str:
        if self.root.resolve() != route.root.resolve():
            return f"kept {path}: it selects {self.root}"
        return super().publish(current, route, path, publish, directory)


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
        return current.route_file()

    def selection(self) -> RouteFileSelection:
        return NoRouteSelection()

    def launch_root(self) -> Path:
        """Where install runs: the default root, where it creates a bus."""
        return LocalRoute.default().root

    def writer(self, path: Path, directory: int) -> DirectoryAtomicFile:
        return DirectoryAtomicFile(path, directory=directory)

    def replace(self, path: Path, publish: Callable[[], None], directory: int) -> Path:
        name = Path(self.filename)
        backup = path.with_name(
            f"{name.stem}.{time.strftime('%Y%m%dT%H%M%S')}-{ScratchName.mint()}.bak{name.suffix}")
        OwnedDirectoryRole.held(directory, path.parent,
                                ValueError("active comms route directory changed"))
        Platform.current().snapshot_file(path.name, backup.name, src_dir_fd=directory,
                                         dst_dir_fd=directory)
        try:
            publish()
        except (OSError, ValueError) as error:
            raise RelationViolationError(
                "active comms route publication outcome UNKNOWN after replacement") from error
        return backup

    def route_new_bus(self, route: ActiveRoute, path: Path,
                      publish: Callable[[], None], directory: int) -> str:
        return f"replaced {path}; old route kept at {self.replace(path, publish, directory)}"

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
    def writer(self, path: Path, directory: int) -> DirectoryAtomicFile:
        return ExclusiveDirectoryAtomicFile(path, directory=directory)

    def route_new_bus(self, route: ActiveRoute, path: Path,
                      publish: Callable[[], None], directory: int) -> str:
        try:
            publish()
        except FileExistsError as error:
            raise ValueError("active comms route is already installed") from error
        except (OSError, ValueError) as error:
            raise RelationViolationError(
                "active comms route publication outcome UNKNOWN after link") from error
        return f"published {path}"


@dataclass(frozen=True)
class SelectingRoute(RouteFile):
    """Still selects its own bus: it is kept, and install runs there."""

    current: ActiveRoute

    def selection(self) -> RouteFileSelection:
        return PublishedRouteSelection(self.current.observe_root())

    def launch_root(self) -> Path:
        return self.current.root

    def route_new_bus(self, route: ActiveRoute, path: Path,
                      publish: Callable[[], None], directory: int) -> str:
        return f"kept {path}: it selects {self.current.root}"
