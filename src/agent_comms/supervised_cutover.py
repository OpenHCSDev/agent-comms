"""Read-only, Linux-specific owner inventory for a supervised root migration.

This is a witness, not a stop permission. The operator must recapture it after
quiescence and preserve the old wire before any route is installed.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .declarations import RelationViolationError, Thread
from .input_disposition import InputDispositions
from .operations import Comms


@dataclass(frozen=True, slots=True)
class OwnerWitness:
    name: str
    pid: int
    created_at: float
    admission_generation: int
    process_start_ticks: int
    agent_bin: str
    agent_args: str


@dataclass(frozen=True, slots=True)
class LegacyInventory:
    root: Path
    live_owners: tuple[OwnerWitness, ...]
    dead_registry_owners: tuple[str, ...]
    active_turns: tuple[str, ...]
    pending_by_thread: tuple[tuple[str, int], ...]
    unknown_inputs: int


@dataclass(frozen=True, slots=True)
class ArchiveReceipt:
    path: Path
    files: int
    pending_messages: int
    unknown_inputs: int


def _process_start_ticks(pid: int) -> int | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes()
    except FileNotFoundError:
        return None
    end = raw.rfind(b") ")
    if end < 0 or raw[: raw.find(b" ")] != str(pid).encode():
        raise RelationViolationError("Owner process identity has malformed proc status")
    fields = raw[end + 2 :].split()
    if len(fields) <= 19:
        raise RelationViolationError("Owner process status omits its start time")
    if fields[0] == b"Z":
        return None
    return int(fields[19])


def _owner_witness(comms: Comms, thread: Thread, generation: int) -> OwnerWitness | None:
    start = _process_start_ticks(thread.pid)
    if start is None:
        return None
    if not comms._is_local_participant(thread, wait=False):
        raise RelationViolationError(f"Live owner {thread.name!r} is not bound to this wire")
    try:
        entries = Path(f"/proc/{thread.pid}/environ").read_bytes().split(b"\0")
    except OSError as error:
        raise RelationViolationError("Cannot inspect the live owner's launch settings") from error
    environment = dict(item.split(b"=", 1) for item in entries if b"=" in item)
    try:
        agent_bin = environment[b"AGENT_COMMS_AGENT_BIN"].decode("utf-8")
        agent_args = environment.get(b"AGENT_COMMS_AGENT_ARGS", b"").decode("utf-8")
    except (KeyError, UnicodeError) as error:
        raise RelationViolationError("Live owner has no reusable agent launch settings") from error
    if not agent_bin or start != _process_start_ticks(thread.pid):
        raise RelationViolationError("Owner process changed during inventory")
    return OwnerWitness(
        thread.name,
        thread.pid,
        thread.created_at,
        generation,
        start,
        agent_bin,
        agent_args,
    )


def inventory_legacy_root(comms: Comms) -> LegacyInventory:
    """Read existing authorities and fail on owner identity races.

    Pending/UNKNOWN counts are advisory while old writers run; the final
    stop barrier must take a fresh immutable archive before switching roots.
    """
    if not sys.platform.startswith("linux") or comms.root.resolve() != Path.home() / ".agent-comms":
        raise ValueError("Legacy owner inventory requires the local Linux comms root")
    before = comms.registry.snapshot()
    owners: list[OwnerWitness] = []
    dead: list[str] = []
    active_turns: list[str] = []
    pending: list[tuple[str, int]] = []
    for name, thread in sorted(before.threads.items()):
        if thread.active_turn is not None:
            active_turns.append(name)
        undelivered = len(comms.inbox(name))
        if undelivered:
            pending.append((name, undelivered))
        if not before.statuses[name].active or not thread.role.executable:
            continue
        if thread.pid <= 0:
            dead.append(name)
            continue
        generation = before.admission_generations.get(name)
        if generation is None:
            raise RelationViolationError("Live owner has no admission generation")
        witness = _owner_witness(comms, thread, generation)
        if witness is None:
            dead.append(name)
        else:
            owners.append(witness)
    after = comms.registry.snapshot()
    if (
        before.threads != after.threads
        or before.statuses != after.statuses
        or before.admission_generations != after.admission_generations
    ):
        raise RelationViolationError("Legacy registry changed during cutover inventory")
    rows = InputDispositions(comms.root)._read()
    return LegacyInventory(
        comms.root,
        tuple(owners),
        tuple(dead),
        tuple(active_turns),
        tuple(pending),
        sum(row["status"] == "unknown" for row in rows.values()),
    )


def _state_files(root: Path) -> tuple[Path, ...]:
    """Capture top-level wire/state stores, including SQLite recovery sidecars."""
    suffixes = (".json", ".jsonl", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".sqlite3-journal")
    return tuple(sorted(path for path in root.iterdir() if path.name.endswith(suffixes)))


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _copy_checked(source: Path, target: Path) -> dict[str, object]:
    descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        initial = os.fstat(descriptor)
        if not stat.S_ISREG(initial.st_mode) or initial.st_uid != os.geteuid():
            raise RelationViolationError("Cutover state source is not an owned regular file")
        output = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        digest = hashlib.sha256()
        try:
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    view = view[os.write(output, view) :]
            os.fsync(output)
        finally:
            os.close(output)
        current = os.fstat(descriptor)
        path_current = source.lstat()
        identity = _file_identity(initial)
        if identity != _file_identity(current) or identity != _file_identity(path_current):
            raise RelationViolationError("Cutover state changed during archive copy")
        return {"size": initial.st_size, "sha256": digest.hexdigest()}
    finally:
        os.close(descriptor)


def archive_stopped_root(comms: Comms, destination: Path) -> ArchiveReceipt:
    """Archive a stopped wire without changing its original rows or ACK cursors.

    This is an immutable evidence copy, not migration or permission to replay.
    It refuses a still-running owner and any registry transition during copy.
    """
    destination = Path(destination).absolute()
    if destination.is_relative_to(comms.root.absolute()):
        raise ValueError("Cutover archive must be outside the old root")
    if destination.exists() or destination.is_symlink():
        raise ValueError("Cutover archive destination already exists")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = destination.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.geteuid()
        or stat.S_IMODE(parent.st_mode) != 0o700
    ):
        raise ValueError("Cutover archive parent must be owner-only")
    before = comms.registry.snapshot()
    if any(thread.active_turn is not None for thread in before.threads.values()):
        raise RelationViolationError("Cutover archive has an active owner turn")
    if any(
        before.statuses[name].active and thread.pid > 0 and comms._process_alive(thread.pid)
        for name, thread in before.threads.items()
    ):
        raise RelationViolationError("Cutover archive requires all old owners stopped")
    files = _state_files(comms.root)
    required = {"bus.jsonl", "bus_meta.json", "registry.json", "input_dispositions.json"}
    if not required.issubset({path.name for path in files}):
        raise RelationViolationError("Cutover archive is missing core wire or input state")
    identities = {path: _file_identity(path.lstat()) for path in files}
    pending = sum(len(comms.inbox(name)) for name in before.threads)
    unknown = sum(
        row["status"] == "unknown" for row in InputDispositions(comms.root)._read().values()
    )
    stage = Path(tempfile.mkdtemp(prefix=".cutover-archive-", dir=destination.parent))
    try:
        entries = {source.name: _copy_checked(source, stage / source.name) for source in files}
        after = comms.registry.snapshot()
        if (
            before.threads != after.threads
            or before.statuses != after.statuses
            or before.admission_generations != after.admission_generations
            or files != _state_files(comms.root)
            or any(
                _file_identity(path.lstat()) != identity for path, identity in identities.items()
            )
        ):
            raise RelationViolationError("Cutover root changed during archive")
        manifest = {
            "version": 1,
            "source_root": str(comms.root.absolute()),
            "captured_at_unix": time.time(),
            "pending_messages": pending,
            "unknown_inputs": unknown,
            "files": entries,
        }
        fd = os.open(stage / ".archive-manifest", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            payload = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
            view = memoryview(payload)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        directory = os.open(stage, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        os.replace(stage, destination)
        directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return ArchiveReceipt(destination, len(files), pending, unknown)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
