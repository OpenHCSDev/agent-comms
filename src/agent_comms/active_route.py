"""Owner-installed default route for a supervised private-root migration.

An absent route retains the historical default. A present but invalid route
fails closed; it never sends a message to a guessed root.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from .bus_publication import unique_wire_object
from .errors import RelationViolationError
from .field_codec import FieldCodec, PathText
from .wire_metadata import WireRootIdText

if TYPE_CHECKING:
    from .owner_lifecycle import OwnerLifecycle


@dataclass(frozen=True, slots=True)
class CommsRoute(ABC):
    """A resolved selection, not a service or permission to mutate its root."""

    root: Path

    def observe_root(self) -> Path:
        return self.root.resolve()

    @contextmanager
    def admit_client(self) -> Iterator[None]:
        """Borrow the published route until this client's work has retired.

        An explicit selection of the published root is still its borrower.
        Independent private roots do not participate in default publication.
        Refuse a publication in progress rather than park a half-open client.
        """
        published = read_active_route()
        current_root = published.root if published else Path.home() / ".agent-comms"
        if self.root.resolve() == current_root.resolve():
            with guard_default_route_write(self.root, blocking=False):
                yield
        else:
            yield

    @abstractmethod
    def bind_owners(self, owners: OwnerLifecycle) -> None:
        """Configure launch authority when a caller actually constructs a service."""


@dataclass(frozen=True, slots=True)
class LocalRoute(CommsRoute):
    """Explicit/environment roots and the unconfigured historical default."""

    def bind_owners(self, owners: OwnerLifecycle) -> None:
        from .private_nk_entrypoint import PrivateNkLaunch

        launch = PrivateNkLaunch.from_environment(self.root, os.environ)
        if launch is not None:
            owners.pin_private_nk_launch(
                launch.validated_root, launch.wire_root_id, launch.native_package
            )


class AbsoluteRoutePathText(PathText):
    """Root and native-package fields share the same absolute-path boundary."""

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
    root: Annotated[Path, AbsoluteRoutePathText]
    wire_root_id: Annotated[str, WireRootIdText]
    native_package: Annotated[Path, AbsoluteRoutePathText]
    version: Literal[1] = field(default=1, kw_only=True, metadata={"wire_required": True})

    @classmethod
    def from_record(cls, value: object) -> ActiveRoute:
        try:
            route = FieldCodec.decode(cls, value)
        except TypeError as error:
            raise ValueError(f"active comms route is invalid: {error}") from error
        if not route.root.is_dir():
            raise ValueError("active comms route root is missing")
        return route

    def observe_root(self) -> Path:
        from .wire_log import WireLog

        # The writer atomically replaces this marker. Reuse its owner/mode,
        # ancestry and protocol decoder, without taking the mutation/durability
        # barrier or interpreting registry/history merely to observe identity.
        marker = WireLog(self.root / "bus.jsonl")._private_marker_unlocked()
        if marker.root_id != self.wire_root_id:
            raise RelationViolationError("private route root ID changed")
        return super(ActiveRoute, self).observe_root()

    def bind_owners(self, owners: OwnerLifecycle) -> None:
        # Actual service binding retains the bus-locked durable verification.
        owners.pin_private_nk_launch(self.root, self.wire_root_id, self.native_package)


def resolve_comms_route(root: Path | str | None = None) -> CommsRoute:
    """Resolve one current selection without creating stores or reading registry."""
    if root is not None:
        return LocalRoute(Path(root).expanduser().absolute())
    if "AGENT_COMMS_ROOT" in os.environ:
        return LocalRoute(Path(os.environ["AGENT_COMMS_ROOT"]).expanduser().absolute())
    return read_active_route() or LocalRoute(Path.home() / ".agent-comms")


class RoutePublicationUnknownError(RelationViolationError):
    """The new route may be visible after a failed durability boundary."""


@contextmanager
def guard_original_root_write(root: Path) -> Iterator[None]:
    """Fence a cooperating write through the historical root after migration."""
    original_root = Path.home() / ".agent-comms"
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
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
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
        value = json.loads(raw, object_pairs_hook=unique_wire_object)
    except (ValueError, UnicodeError) as error:
        raise ValueError("active comms route is invalid JSON") from error
    return ActiveRoute.from_record(value)


@contextmanager
def guard_default_route_write(expected_root: Path, *, blocking: bool = True) -> Iterator[None]:
    """Keep a default-root write on its selected root through publication.

    Callers must enter this guard before the mutating operation, including any
    thread dispatch. Nested shared borrowers remain compatible and each owns
    its descriptor; their lifetime is not inferred from thread-local state.
    """
    path = active_route_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
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
        route = read_active_route(path)
        current_root = route.root if route is not None else Path.home() / ".agent-comms"
        if expected_root.expanduser().resolve(strict=True) != current_root.resolve(strict=True):
            raise ValueError("default comms route changed before write")
        yield
    finally:
        os.close(directory)


def publish_active_route(route: ActiveRoute, path: Path | None = None) -> None:
    """Atomically install the first private default after owner migration.

    Existing routes are never overwritten by a stale migration. Publication
    uses the same no-replace hardlink pattern as the private store initializer;
    a reader fails closed during the brief two-link staging window.
    """
    from .cohort_foreground import _preflight

    path = active_route_path() if path is None else path
    if path.name != "active-route.json" or not path.is_absolute():
        raise ValueError("active comms route requires its absolute route path")
    FieldCodec.encode(route)
    _preflight(route.root, route.wire_root_id, route.native_package, True)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        _publish_active_route_locked(route, path, directory)
    finally:
        os.close(directory)


def _publish_active_route_locked(
    route: ActiveRoute,
    path: Path,
    directory: int,
    *,
    expected: ActiveRoute | None = None,
) -> None:
    """Publish through a checked directory FD already held with route EX."""
    from .cohort_foreground import _preflight

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
        current = read_active_route(path)
        if current != expected:
            raise ValueError(
                "active comms route is already installed"
                if expected is None
                else "active comms route is not the expected private root"
            )
        payload = (
            json.dumps(FieldCodec.encode(route), sort_keys=True)
            + "\n"
        ).encode()
        temporary = f".active-route-{uuid.uuid4().hex}.tmp"
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
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
        _preflight(route.root, route.wire_root_id, route.native_package, True)
        parent = path.parent.lstat()
        if (parent.st_dev, parent.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("active comms route directory changed before publication")
        if expected is None:
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
            if read_active_route(path) != expected:
                raise ValueError("active comms route changed before replacement")
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
            with suppress(OSError):
                os.unlink(temporary, dir_fd=directory)
