"""Owner-installed default route for a supervised private-root cutover.

An absent route retains the historical default. A present but invalid route
fails closed; it never sends a message to a guessed root.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .declarations import unique_wire_object


@dataclass(frozen=True, slots=True)
class ActiveRoute:
    root: Path
    wire_root_id: str
    native_package: Path


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
