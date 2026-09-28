"""Bus durability: declaration and persistence owners."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from .bus_publication import PRIVATE_WIRE_FIELD, unique_wire_object
from .errors import RelationViolationError
from .messages import Message


def _claim_gate_path(bus_path: Path) -> Path:
    """The EXISTING private bus protocol marker; no second ownership file."""
    return bus_path.with_name("bus_meta.json")


def _claim_gate_enabled(bus_path: Path) -> bool:
    # _store_lock is also used for registry, channels, and marker files.
    # Only the canonical bus may enter this read/durability barrier.
    if bus_path.name != "bus.jsonl":
        return False
    marker = _claim_gate_path(bus_path)
    checkpoint_path = bus_path.with_name("private_bus_checkpoint.sqlite3")
    checkpoint_present = checkpoint_path.exists() or checkpoint_path.is_symlink()
    try:
        metadata = json.loads(marker.read_text(), object_pairs_hook=unique_wire_object)
    except FileNotFoundError as error:
        if checkpoint_present:
            raise RelationViolationError(
                "Private checkpoint has no durable protocol marker."
            ) from error
        return False
    except (ValueError, UnicodeError) as error:
        raise RelationViolationError("Bus protocol marker is malformed.") from error
    if type(metadata) is not dict:
        raise RelationViolationError("Bus protocol marker is not an object.")
    version = metadata.get("claim_envelopes_version")
    if version is None:
        if checkpoint_present or "checkpoint_version" in metadata or "checkpoint_seal" in metadata:
            raise RelationViolationError("Private checkpoint lacks its claim read barrier.")
        return False
    if type(version) is not int or version != 1:
        raise RelationViolationError("Unsupported claim-envelope protocol marker.")
    return True


def _verify_claim_bus_before_read_unlocked(bus_path: Path) -> None:
    """Make every visible opt-in bus row durable before ANY bus-lock reader sees it.

    The marker is fsynced before the first claim send. A failed bus append may
    leave a complete, visible row: another cooperating process must fsync the
    opened inode and its directory, then reject incomplete/corrupt rows, before
    treating either the announcement or the claim as committed. This hook is
    entered by the shared bus lock, including ordinary inbox/history readers.
    """
    if not _claim_gate_enabled(bus_path):
        return
    marker = _claim_gate_path(bus_path)
    marker_info = marker.lstat()
    if (
        os.name != "posix"
        or not stat.S_ISREG(marker_info.st_mode)
        or marker_info.st_uid != os.geteuid()
        or stat.S_IMODE(marker_info.st_mode) != 0o600
    ):
        raise RelationViolationError("Claim bus read barrier is not durable and private.")
    try:
        # The private marker's source of truth is its single fsynced JSON file.
        # The root identity/sequence/claim flag are validated again by private
        # writers; this preflight protects all ordinary readers on marked roots.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(bus_path, flags)
        except FileNotFoundError:
            descriptor = None
        if descriptor is None and (
            (bus_path.with_name("private_bus_checkpoint.sqlite3")).exists()
            or (bus_path.with_name("private_bus_checkpoint.sqlite3")).is_symlink()
            or any(
                key in json.loads(marker.read_text(), object_pairs_hook=unique_wire_object)
                for key in ("checkpoint_version", "checkpoint_seal")
            )
        ):
            raise RelationViolationError("Private checkpoint bus inode is missing.")
        if descriptor is not None:
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise RelationViolationError("Claim bus is not a regular file.")
                os.fsync(stream.fileno())
                from .private_bus_checkpoint import (
                    certificate_enabled,
                    verify_private_bus_checkpoint_unlocked,
                )

                if certificate_enabled(bus_path) or any(
                    key in json.loads(marker.read_text(), object_pairs_hook=unique_wire_object)
                    for key in ("checkpoint_version", "checkpoint_seal")
                ):
                    # The caller already owns the bus lock. Do not instantiate
                    # a registry (or recursively acquire a store lock) here.
                    from .message_bus import MessageBus

                    bus = MessageBus.__new__(MessageBus)
                    bus._path = bus_path
                    private_marker = bus._private_marker_unlocked()
                    if (
                        private_marker.get("checkpoint_version") != 1
                        or "checkpoint_seal" not in private_marker
                    ):
                        raise RelationViolationError(
                            "Private checkpoint lacks durable marker binding."
                        )
                    directory_fd = os.open(
                        bus_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                    )
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                    verify_private_bus_checkpoint_unlocked(bus, private_marker)
                    return
                while line := stream.readline(8 * 1024 * 1024 + 1):
                    if len(line) > 8 * 1024 * 1024 or not line.endswith(b"\n"):
                        raise RelationViolationError("Incomplete or oversized claim bus row.")
                    try:
                        row = json.loads(line, object_pairs_hook=unique_wire_object)
                    except (ValueError, UnicodeError) as error:
                        raise RelationViolationError("Malformed claim bus row.") from error
                    if not isinstance(row, dict):
                        raise RelationViolationError("Claim bus row must be an object.")
                    if "claim_transition" in row:
                        try:
                            public = Message.from_wire(row).to_wire()
                        except (KeyError, TypeError, ValueError, AttributeError) as error:
                            raise RelationViolationError("Malformed claim envelope.") from error
                        if {
                            key: value for key, value in row.items() if key != PRIVATE_WIRE_FIELD
                        } != public:
                            raise RelationViolationError("Noncanonical claim envelope.")
        directory_fd = os.open(bus_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as error:
        raise RelationViolationError("Claim bus durability is UNKNOWN.") from error
