"""Prototype of a root-owned maintenance witness reader (NO operator writer).

Not wired into ordinary workers: an explicit, root-owned deployment selection and
OS-separated worker identities are prerequisites. In particular, the current
uid1000 account has passwordless root and this module cannot authenticate it.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

from .declarations import RelationViolationError, unique_wire_object

# The only production trust anchors. Tests substitute disposable paths/identities;
# doing so is NOT an operational authority or old-import exclusion test.
_CONFIG_BASE = Path("/etc/agent-comms/maintenance")
_STATE_BASE = Path("/var/lib/agent-comms/maintenance")
_TRUSTED_OWNER_UID = 0
_CONFIG_OWNER_GID = 0
_ANCESTOR_STOP = Path("/")
_ROOT_ID = re.compile(r"[0-9a-f]{32}\Z")


def _closed(message: str) -> RelationViolationError:
    return RelationViolationError(f"Maintenance protected witness {message}; admission closed")


def _parents_trusted(path: Path) -> None:
    """Check every ancestor through the trusted root; no writable/symlink path."""
    stop = _ANCESTOR_STOP
    if not path.is_absolute() or not stop.is_absolute() or not path.is_relative_to(stop):
        raise _closed("path escapes its trust anchor")
    parent = path.parent
    while True:
        try:
            info = parent.lstat()
        except OSError as error:
            raise _closed("ancestor is unavailable") from error
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != _TRUSTED_OWNER_UID
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise _closed("ancestor is not protected")
        if parent == stop:
            break
        parent = parent.parent


def _json_file(path: Path, *, group: int, mode: int) -> dict[str, object]:
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise _closed("requires POSIX no-follow open")
    _parents_trusted(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except OSError as error:
        raise _closed("is missing or inaccessible") from error
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != _TRUSTED_OWNER_UID
            or info.st_gid != group
            or stat.S_IMODE(info.st_mode) != mode
            or info.st_nlink != 1
            or not 0 < info.st_size <= 4096
        ):
            raise _closed("has invalid type, owner, group or mode")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            value = json.load(stream, object_pairs_hook=unique_wire_object)
    except (OSError, UnicodeError, ValueError) as error:
        raise _closed("is malformed") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if type(value) is not dict:
        raise _closed("is not an object")
    return value


def read_protected(registry_path: Path, config_path: Path) -> tuple[int, str, str, str]:
    """Read a configured phase, never falling back to the user-owned witnesses.

    This remains synchronous so callers can hold their existing wire/registry
    lock over the check and irreversible operation. There is deliberately no
    begin, CAS writer, recovery or reopen method here.
    """
    config_path = Path(config_path)
    if config_path.parent != _CONFIG_BASE:
        raise _closed("configuration is outside its fixed root")
    config = _json_file(config_path, group=_CONFIG_OWNER_GID, mode=0o644)
    if set(config) != {"version", "required", "root", "root_id", "state_dir", "reader_gid"}:
        raise _closed("configuration fields are invalid")
    root_id = config["root_id"]
    state_dir = config["state_dir"]
    reader_gid = config["reader_gid"]
    root = str(Path(registry_path).parent.resolve())
    if (
        type(config["version"]) is not int
        or config["version"] != 1
        or config["required"] is not True
        or type(root_id) is not str
        or not _ROOT_ID.fullmatch(root_id)
        or config_path.name != root_id + ".json"
        or config["root"] != root
        or type(state_dir) is not str
        or state_dir != str(_STATE_BASE / root_id)
        or type(reader_gid) is not int
        or not 0 < reader_gid < 1 << 31
        or reader_gid not in {os.getegid(), *os.getgroups()}
    ):
        raise _closed("configuration is inconsistent with this worker/root")
    phase_dir = Path(state_dir)
    _parents_trusted(phase_dir)
    try:
        directory = phase_dir.lstat()
    except OSError as error:
        raise _closed("phase directory is missing") from error
    if (
        not stat.S_ISDIR(directory.st_mode)
        or directory.st_uid != _TRUSTED_OWNER_UID
        or directory.st_gid != reader_gid
        or stat.S_IMODE(directory.st_mode) != 0o750
    ):
        raise _closed("phase directory is not protected")
    marker = _json_file(phase_dir / "enabled", group=reader_gid, mode=0o640)
    state = _json_file(phase_dir / "state.json", group=reader_gid, mode=0o640)
    expected = {"version", "root", "root_id", "generation"}
    if set(marker) != expected or set(state) != expected | {"operator", "nonce", "phase"}:
        raise _closed("phase fields are invalid")
    generation = marker["generation"]
    if (
        type(marker["version"]) is not int
        or marker["version"] != 1
        or type(state["version"]) is not int
        or state["version"] != 1
        or marker["root"] != root
        or state["root"] != root
        or marker["root_id"] != root_id
        or state["root_id"] != root_id
        or type(generation) is not int
        or not 0 < generation < 1 << 63
        or state["generation"] != generation
        or type(state["operator"]) is not str
        or not state["operator"]
        or type(state["nonce"]) is not str
        or not re.fullmatch(r"[0-9a-f]{32}", state["nonce"])
        or type(state["phase"]) is not str
        or state["phase"] not in ("draining", "paused", "installing", "ready")
    ):
        raise _closed("phase is inconsistent")
    return generation, state["operator"], state["nonce"], state["phase"]
