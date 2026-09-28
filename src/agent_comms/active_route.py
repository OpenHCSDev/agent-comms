"""Owner-installed default route for a supervised private-root cutover.

An absent route retains the historical default. A present but invalid route
fails closed; it never sends a message to a guessed root.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import threading
import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .bus_publication import unique_wire_object
from .errors import RelationViolationError

if TYPE_CHECKING:
    from .owner_lifecycle import OwnerLifecycle


@dataclass(frozen=True, slots=True)
class CommsRoute(ABC):
    """A resolved selection, not a service or permission to mutate its root."""

    root: Path

    def observe_root(self) -> Path:
        return self.root.resolve()

    @abstractmethod
    def bind_owners(self, owners: OwnerLifecycle) -> None:
        """Configure launch authority when a caller actually constructs a service."""


@dataclass(frozen=True, slots=True)
class LocalRoute(CommsRoute):
    """Explicit/environment roots and the unconfigured historical default."""

    def bind_owners(self, owners: OwnerLifecycle) -> None:
        pass


@dataclass(frozen=True, slots=True)
class ActiveRoute(CommsRoute):
    wire_root_id: str
    native_package: Path

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
        return LocalRoute(Path(root).expanduser())
    if "AGENT_COMMS_ROOT" in os.environ:
        return LocalRoute(Path(os.environ["AGENT_COMMS_ROOT"]).expanduser())
    return read_active_route() or LocalRoute(Path.home() / ".agent-comms")


class RoutePublicationUnknownError(RelationViolationError):
    """The new route may be visible after a failed durability boundary."""


_route_write_owner = threading.local()


@contextmanager
def guard_original_root_write(root: Path) -> Iterator[None]:
    """Fence a cooperating write through the historical root after cutover."""
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
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "root", "wire_root_id", "native_package"}
        or type(value["version"]) is not int
        or value["version"] != 1
    ):
        raise ValueError("active comms route has an unsupported shape")
    root, root_id, package = (value["root"], value["wire_root_id"], value["native_package"])
    if (
        type(root) is not str
        or type(package) is not str
        or not root
        or not package
        or not Path(root).is_absolute()
        or not Path(package).is_absolute()
        or ".." in Path(root).parts
        or ".." in Path(package).parts
        or type(root_id) is not str
        or len(root_id) != 32
        or any(ch not in "0123456789abcdef" for ch in root_id)
    ):
        raise ValueError("active comms route has invalid absolute identities")
    if not Path(root).is_dir():
        raise ValueError("active comms route root is missing")
    return ActiveRoute(Path(root), root_id, Path(package))


@contextmanager
def guard_default_route_write(expected_root: Path) -> Iterator[None]:
    """Keep a default-root write on its selected root through publication.

    Callers must enter this guard before the mutating operation, including any
    thread dispatch. A nested guard in the same thread shares the outer lock.
    """
    path = active_route_path()
    held = getattr(_route_write_owner, "held", None)
    identity = (str(path), str(expected_root.expanduser().resolve(strict=True)))
    if held is not None:
        if held != identity:
            raise ValueError("nested default comms write changed its route")
        yield
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    try:
        fcntl.flock(directory, fcntl.LOCK_SH)
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
        _route_write_owner.held = identity
        try:
            yield
        finally:
            _route_write_owner.held = None
    finally:
        os.close(directory)


def publish_active_route(route: ActiveRoute, path: Path | None = None) -> None:
    """Atomically install the first private default after owner cutover.

    Existing routes are never overwritten by a stale cutover. Publication
    uses the same no-replace hardlink pattern as the private store initializer;
    a reader fails closed during the brief two-link staging window.
    """
    from .cohort_foreground import _preflight

    path = active_route_path() if path is None else path
    if path.name != "active-route.json" or not path.is_absolute():
        raise ValueError("active comms route requires its absolute route path")
    if (
        not route.root.is_absolute()
        or not route.native_package.is_absolute()
        or ".." in route.root.parts
        or ".." in route.native_package.parts
    ):
        raise ValueError("active comms route requires absolute root and package identities")
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
            json.dumps(
                {
                    "version": 1,
                    "root": str(route.root),
                    "wire_root_id": route.wire_root_id,
                    "native_package": str(route.native_package),
                },
                sort_keys=True,
            )
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
