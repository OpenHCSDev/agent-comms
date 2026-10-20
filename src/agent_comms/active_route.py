"""Owner-installed default route for a supervised private-root migration.

An absent route retains the historical default. A present but invalid route
fails closed; it never sends a message to a guessed root.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import sys
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, ClassVar

from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import FieldCodec, Id128Text, MalformedRecord, PathText
from .launch_root import LaunchRoot
from .private_nk_entrypoint import PrivateNkLaunch, RootLaunch


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

    @contextmanager
    def hold_client_route(self) -> Iterator[None]:
        """Borrow the published route until this client's work has retired.

        An explicit selection of the published root is still its borrower.
        Independent private roots do not participate in default publication.
        Refuse a publication in progress rather than park a half-open client.
        """
        current_root = CommsRoute.current().root
        if self.root.resolve() == current_root.resolve():
            with guard_default_route_write(self.root, blocking=False):
                yield
        else:
            yield



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
    wire_root_id: Annotated[str, Id128Text]
    # The file format: the writer stamps it and the reader refuses any other before decoding.
    version: ClassVar[int] = 2
    # The CLI command that replaces a file this reader refuses; it registers under this name.
    repair_command: ClassVar[str] = "replace-route"

    @classmethod
    def published(cls) -> ActiveRoute:
        match read_active_route():
            case ActiveRoute() as route:
                return route
            case _:
                raise RelationViolationError(
                    "No active Comms route is published; --comms-root or --root is required."
                )

    def to_record(self) -> dict[str, object]:
        return {"version": self.version, **FieldCodec.encode(self)}

    @classmethod
    def from_record(cls, value: object, path: Path) -> ActiveRoute:
        match value:
            case {"version": int(found), **record} if found == cls.version:
                pass
            case _:
                stamped = value.get("version") if isinstance(value, dict) else None
                raise MalformedRecord(
                    f"active comms route {path} is version {stamped!r}; "
                    f"this installation reads version {cls.version}. "
                    "Replace it with the bus it should select: "
                    f"agent-comms --root ROOT {cls.repair_command}")
        try:
            route = FieldCodec.decode(cls, record)
        except MalformedRecord as error:
            raise MalformedRecord(f"active comms route {path} is invalid: {error}") from error
        if not route.root.is_dir():
            raise ValueError("active comms route root is missing")
        return route

    def observe_root(self) -> Path:
        from .wire_log import WireLog

        # The writer atomically replaces this marker. Reuse its owner/mode,
        # ancestry and protocol decoder, without taking the mutation/durability
        # barrier or interpreting registry/history merely to observe identity.
        marker = WireLog(self.root / "bus.jsonl").private_marker_unlocked()
        if marker.root_id != self.wire_root_id:
            raise RelationViolationError("private route root ID changed")
        return super(ActiveRoute, self).observe_root()

    def launch(self) -> PrivateNkLaunch:
        return PrivateNkLaunch.installed(self.root, self.wire_root_id)


def resolve_comms_route(root: CommsRoute | Path | str | None = None) -> CommsRoute:
    """Resolve one current selection without creating stores or reading registry."""
    if isinstance(root, CommsRoute):
        return root
    if root is not None:
        return LocalRoute(Path(root).expanduser().absolute())
    return LaunchRoot.current_route()


class RoutePublicationUnknownError(RelationViolationError):
    """The new route may be visible after a failed durability boundary."""


@contextmanager
def guard_original_root_write(root: Path) -> Iterator[None]:
    """Fence a cooperating write through the historical root after migration."""
    original_root = LocalRoute.default().root
    if root.expanduser().resolve() == original_root.resolve():
        with guard_default_route_write(original_root):
            yield
    else:
        yield


def active_route_path() -> Path:
    return Path.home() / ".local/state/agent-comms/active-route.json"


def read_active_route(path: Path | None = None) -> ActiveRoute | None:
    """Decode the one trusted default route; explicit roots bypass this file."""
    path = active_route_path() if path is None else path
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        parent = path.parent.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) != 0o700
        ):
            raise ValueError("active comms route directory must be owner-only")
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or not 0 < info.st_size <= 4096
        ):
            raise ValueError("active comms route must be one owner-only regular file")
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


@contextmanager
def guard_default_route_write(expected_root: Path, *, blocking: bool = True) -> Iterator[None]:
    """Keep a default-root write on its selected root through publication.

    Callers must enter this guard before the mutating operation, including any
    thread dispatch. Nested shared borrowers remain compatible and each owns
    its descriptor; their lifetime is not inferred from thread-local state.
    """
    path = active_route_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(directory, fcntl.LOCK_SH | (0 if blocking else fcntl.LOCK_NB))
        info = os.fstat(directory)
        parent = path.parent.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or (parent.st_dev, parent.st_ino) != (info.st_dev, info.st_ino)
        ):
            raise ValueError("active comms route directory changed or is not owned")
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.fchmod(directory, 0o700)
        route = CommsRoute.current(path)
        # Local roots may be created under this guard. ActiveRoute owns the
        # stronger observation of an already published root and its identity.
        if expected_root.expanduser().resolve() != route.observe_root():
            raise ValueError("default comms route changed before write")
        yield
    finally:
        os.close(directory)


class RouteLockHolder(DeclaredFamily, affix="LockHolder"):
    """An observed Linux flock owner; diagnosis, not authority to signal it."""

    @abstractmethod
    def describe(self) -> str: ...


@dataclass(frozen=True, slots=True)
class IdentifiedLockHolder(RouteLockHolder):
    pid: int

    def describe(self) -> str:
        return f"PID {self.pid}: {self.detail()}"

    @abstractmethod
    def detail(self) -> str: ...


@dataclass(frozen=True, slots=True)
class RunningLockHolder(IdentifiedLockHolder):
    command: str

    def detail(self) -> str:
        return self.command


class ExitedLockHolder(IdentifiedLockHolder):
    def detail(self) -> str:
        return "process exited"


class EmptyCommandLockHolder(IdentifiedLockHolder):
    def detail(self) -> str:
        return "empty command"


class UnidentifiedLockHolder(RouteLockHolder):
    def describe(self) -> str:
        return "kernel did not identify the owner"


def route_lock_holders(directory: int) -> tuple[RouteLockHolder, ...]:
    """Identify actual flock owners of this opened directory, not merely open FDs.

    An OFD lock with no reported PID, inaccessible /proc, or a concurrent exit
    is unknown; absence from this snapshot never proves sole ownership of the lock.
    """
    info = os.fstat(directory)
    identity = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino}"
    holders = []
    with open('/proc/locks', encoding='ascii') as locks:
        for line in locks:
            fields = line.split()
            if len(fields) < 8 or fields[1] != 'FLOCK' or fields[5] != identity:
                continue
            pid = int(fields[4])
            if pid < 0:
                holders.append(UnidentifiedLockHolder())
                continue
            try:
                command = Path(f'/proc/{pid}/cmdline').read_bytes().replace(b'\0', b' ').decode('utf-8', 'replace').strip()
            except FileNotFoundError:
                holders.append(ExitedLockHolder(pid))
            else:
                holders.append(RunningLockHolder(pid, command) if command else EmptyCommandLockHolder(pid))
    return tuple(holders)


def acquire_exclusive_route_lock(directory: int) -> None:
    """Name current readers, then wait for the kernel to release their locks."""
    try:
        fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        holders = route_lock_holders(directory)
        detail = '; '.join(holder.describe() for holder in holders)
        print(
            f'Waiting for default-route readers: '
            f'{detail or "unknown (lock changed during inspection)"}',
            file=sys.stderr, flush=True,
        )
        fcntl.flock(directory, fcntl.LOCK_EX)


def publish_active_route(route: ActiveRoute, path: Path | None = None) -> None:
    """Atomically install the first private default after owner migration.

    Existing routes are never overwritten by a stale migration. Publication
    uses the same no-replace hardlink pattern as the private store initializer;
    a reader fails closed during the brief two-link staging window.
    """
    with _route_directory(route, path) as (path, directory):
        _publish_active_route_locked(route, path, directory)


def replace_unreadable_active_route(route: ActiveRoute, path: Path | None = None) -> Path:
    """The owner's explicit repair of an old-version or unreadable route file.

    The old file is kept beside the new one under a backup name; a readable or
    absent route is refused, so a live route is still only ever installed once.
    """
    with _route_directory(route, path) as (path, directory):
        try:
            current = read_active_route(path)
        except MalformedRecord:
            backup = path.with_name(f"active-route.{time.strftime('%Y%m%dT%H%M%S')}.bak.json")
            _publish_active_route_locked(route, path, directory, backup=backup)
            return backup
        raise ValueError(
            f"active comms route {path} is readable and selects {current.root}; it is not replaced"
            if current is not None else f"active comms route {path} is absent; nothing to replace")


@contextmanager
def _route_directory(route: ActiveRoute, path: Path | None) -> Iterator[tuple[Path, int]]:
    """The checked route path and its directory FD, held with route EX."""
    path = active_route_path() if path is None else path
    if path.name != "active-route.json" or not path.is_absolute():
        raise ValueError("active comms route requires its absolute route path")
    FieldCodec.encode(route)
    route.launch().validate()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        acquire_exclusive_route_lock(directory)
        yield path, directory
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
        info = os.fstat(directory)
        parent = path.parent.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or parent.st_dev != info.st_dev
            or parent.st_ino != info.st_ino
        ):
            raise ValueError("active comms route directory changed or is not owned")
        os.fchmod(directory, 0o700)
        if backup is None and read_active_route(path) is not None:
            raise ValueError("active comms route is already installed")
        payload = (json.dumps(route.to_record(), sort_keys=True) + "\n").encode()
        temporary = f".active-route-{uuid.uuid4().hex}.tmp"
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
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
