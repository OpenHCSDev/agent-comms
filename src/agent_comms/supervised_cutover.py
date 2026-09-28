"""Linux owner inventory and stopped-root archive for a supervised migration.

This is a witness, not a stop permission. The operator must recapture it after
quiescence and preserve the old wire before any route is installed.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from .bus_publication import stable_thread_lookup
from .cohort_schema import install_private_cohort_schema
from .comms import Comms
from .coordinated_runtime_schema import install_native_runtime_schema
from .coordination_response import install_private_response_schema
from .coordination_store import MutationStore
from .errors import RelationViolationError
from .goal_waits import GoalWaits
from .input_disposition import InputDispositions
from .native_prompt_binding import install_prompt_binding_schema
from .private_bus_checkpoint import install_private_bus_checkpoint
from .store_files import _store_lock
from .thread_status import StoppedThreadStatus
from .threads import Thread

if TYPE_CHECKING:
    from .active_route import ActiveRoute


@dataclass(frozen=True, slots=True)
class OwnerWitness:
    name: str
    pid: int
    created_at: float
    admission_generation: int
    process_start_ticks: int
    session_file: Path
    session_device: int
    session_inode: int
    worktree: Path
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
    if not comms.owners._is_local_participant(thread, wait=False):
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
    if not thread.session_file:
        raise RelationViolationError(f"Live owner {thread.name!r} has no saved session")
    session_file = Path(thread.session_file)
    worktree = Path(thread.worktree)
    if not session_file.is_absolute() or not worktree.is_absolute():
        raise RelationViolationError("Owner session and worktree must have absolute paths")
    try:
        session = session_file.lstat()
        workspace = worktree.lstat()
    except OSError as error:
        raise RelationViolationError("Owner saved session or worktree is unavailable") from error
    if (
        not stat.S_ISREG(session.st_mode)
        or session.st_uid != os.geteuid()
        or stat.S_IMODE(session.st_mode) != 0o600
        or session.st_nlink != 1
        or session.st_size == 0
        or not stat.S_ISDIR(workspace.st_mode)
        or workspace.st_uid != os.geteuid()
    ):
        raise RelationViolationError("Owner saved session or worktree lacks handoff integrity")
    if start != _process_start_ticks(thread.pid):
        raise RelationViolationError("Owner process changed during session handoff inventory")
    return OwnerWitness(
        thread.name,
        thread.pid,
        thread.created_at,
        generation,
        start,
        session_file,
        session.st_dev,
        session.st_ino,
        worktree,
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
        undelivered = len(comms.bus.inbox(name))
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
    """Capture wire stores and private Pi journals, excluding transient sockets."""
    suffixes = (".json", ".jsonl", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".sqlite3-journal")
    files = [path for path in root.iterdir() if path.name.endswith(suffixes)]
    for name in ("native-sessions", "runtime"):
        base = root / name
        if not base.exists() and not base.is_symlink():
            continue
        for directory_name, directory_names, file_names in os.walk(base, followlinks=False):
            directory = Path(directory_name)
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                raise RelationViolationError("Cutover private state directory is redirected")
            for child_name in directory_names:
                child = directory / child_name
                child_info = child.lstat()
                if not stat.S_ISDIR(child_info.st_mode) or child_info.st_uid != os.geteuid():
                    raise RelationViolationError("Cutover private state directory is redirected")
            for child_name in file_names:
                child = directory / child_name
                child_info = child.lstat()
                if stat.S_ISSOCK(child_info.st_mode):
                    continue
                if not stat.S_ISREG(child_info.st_mode) or child_info.st_uid != os.geteuid():
                    raise RelationViolationError("Cutover private state file is redirected")
                files.append(child)
    return tuple(sorted(files))


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _copy_checked(source: Path, target: Path) -> dict[str, object]:
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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


def _publish_archive_noreplace(stage: Path, destination: Path) -> None:
    """Publish one complete archive directory without replacing a rival."""
    if not sys.platform.startswith("linux"):
        raise ValueError("Cutover archive publication requires Linux renameat2")
    directory = os.open(
        destination.parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        parent = destination.parent.lstat()
        info = os.fstat(directory)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
            or (parent.st_dev, parent.st_ino) != (info.st_dev, info.st_ino)
        ):
            raise ValueError("Cutover archive parent changed before publication")
        try:
            renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
        except AttributeError as error:
            raise ValueError("Cutover archive cannot guarantee no-replace publication") from error
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        # Linux RENAME_NOREPLACE=1. Both names are relative to the checked
        # owner-only parent fd, so no path-based replace can race this step.
        if (
            renameat2(
                directory, os.fsencode(stage.name), directory, os.fsencode(destination.name), 1
            )
            != 0
        ):
            code = ctypes.get_errno()
            if code in (errno.EEXIST, errno.ENOTEMPTY):
                raise ValueError("Cutover archive destination already exists")
            raise OSError(code, os.strerror(code), str(destination))
        os.fsync(directory)
    finally:
        os.close(directory)


def archive_stopped_root(comms: Comms, destination: Path) -> ArchiveReceipt:
    """Archive a stopped wire without changing its original rows or ACK cursors.

    This is an immutable evidence copy, not migration or permission to replay.
    It refuses a still-running owner and any registry transition during copy.
    """
    destination = Path(destination).absolute()
    if destination.parent.resolve().is_relative_to(comms.root.resolve()):
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
        thread.pid > 0 and comms.owners._process_alive(thread.pid) for thread in before.threads.values()
    ):
        raise RelationViolationError("Cutover archive requires all old owners stopped")
    files = _state_files(comms.root)
    required = {"bus.jsonl", "bus_meta.json", "registry.json"}
    if not required.issubset({path.name for path in files}):
        raise RelationViolationError("Cutover archive is missing core wire or input state")
    identities = {path: _file_identity(path.lstat()) for path in files}
    pending = sum(len(comms.bus.inbox(name)) for name in before.threads)
    unknown = sum(
        row["status"] == "unknown" for row in InputDispositions(comms.root)._read().values()
    )
    stage = Path(tempfile.mkdtemp(prefix=".cutover-archive-", dir=destination.parent))
    try:
        entries = {
            source.relative_to(comms.root).as_posix(): _copy_checked(
                source, stage / source.relative_to(comms.root)
            )
            for source in files
        }
        after = comms.registry.snapshot()
        if (
            before.threads != after.threads
            or before.statuses != after.statuses
            or before.admission_generations != after.admission_generations
            or files != _state_files(comms.root)
            or any(
                _file_identity(path.lstat()) != identity for path, identity in identities.items()
            )
            or any(
                thread.pid > 0 and comms.owners._process_alive(thread.pid)
                for thread in after.threads.values()
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
        directories = {stage}
        directories.update((stage / source.relative_to(comms.root)).parent for source in files)
        for staged_dir in sorted(directories, key=lambda item: len(item.parts), reverse=True):
            directory = os.open(staged_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        _publish_archive_noreplace(stage, destination)
        return ArchiveReceipt(destination, len(files), pending, unknown)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _require_unchanged_archive_source(legacy: Comms, archive: ArchiveReceipt) -> None:
    """Reject any old-root append or state update since the stopped archive."""
    manifest_path = archive.path / ".archive-manifest"
    info = manifest_path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise RelationViolationError("Cutover archive manifest is not owner-only")
    manifest = json.loads(manifest_path.read_text())
    files = manifest.get("files")
    sources = _state_files(legacy.root)
    if (
        manifest.get("version") != 1
        or manifest.get("source_root") != str(legacy.root.absolute())
        or not isinstance(files, dict)
        or set(files) != {path.relative_to(legacy.root).as_posix() for path in sources}
    ):
        raise RelationViolationError("Cutover archive differs from the stopped source")
    for source in sources:
        before = source.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid():
            raise RelationViolationError("Cutover source is not an owned regular file")
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        after = source.lstat()
        if _file_identity(before) != _file_identity(after) or files[
            source.relative_to(legacy.root).as_posix()
        ] != {
            "size": before.st_size,
            "sha256": digest.hexdigest(),
        }:
            raise RelationViolationError("Old root changed after its cutover archive")


def stage_private_participants(
    legacy: Comms,
    private: Comms,
    archive: ArchiveReceipt,
    inventory: LegacyInventory,
    selected_names: Sequence[str],
) -> tuple[str, tuple[OwnerWitness, ...]]:
    """Seed stopped saved-session owners into a fresh private root, without replay.

    This never launches a process or copies legacy messages, ACKs, claims, or
    UNKNOWN dispositions. A partially staged root is discarded, not retried.
    The operator chooses which witnessed owners to stage and later start; the
    returned witnesses retain each owner's verified agent launch settings.
    """
    selected = tuple(selected_names)
    if not selected or len(set(selected)) != len(selected):
        raise ValueError("Cutover participant selection must be nonempty and unique")
    if (
        legacy.root.resolve() != inventory.root.resolve()
        or private.root.resolve() == legacy.root.resolve()
        or private.root.resolve().is_relative_to(legacy.root.resolve())
        or private.root.resolve().is_relative_to(archive.path.resolve())
        or archive.path.resolve().is_relative_to(private.root.resolve())
    ):
        raise ValueError("Cutover source, archive, and private root must be distinct")
    _require_unchanged_archive_source(legacy, archive)
    snapshot = legacy.registry.snapshot()
    if any(thread.active_turn is not None for thread in snapshot.threads.values()) or any(
        thread.pid > 0 and legacy.owners._process_alive(thread.pid) for thread in snapshot.threads.values()
    ):
        raise RelationViolationError("Cutover participants require all old owners stopped")
    witnesses = {witness.name: witness for witness in inventory.live_owners}
    participants: list[Thread] = []
    for name in selected:
        thread = snapshot.threads.get(name)
        witness = witnesses.get(name)
        if (
            thread is None
            or witness is None
            or not thread.role.executable
            or not snapshot.statuses[name].stopped
            or (thread.pid, thread.created_at, thread.session_file, thread.worktree)
            != (
                witness.pid,
                witness.created_at,
                str(witness.session_file),
                str(witness.worktree),
            )
        ):
            raise RelationViolationError(f"Cutover owner {name!r} lacks its stopped witness")
        session = witness.session_file.lstat()
        worktree = witness.worktree.lstat()
        if (
            not stat.S_ISREG(session.st_mode)
            or session.st_uid != os.geteuid()
            or stat.S_IMODE(session.st_mode) != 0o600
            or session.st_nlink != 1
            or session.st_size == 0
            or (session.st_dev, session.st_ino) != (witness.session_device, witness.session_inode)
            or not stat.S_ISDIR(worktree.st_mode)
            or worktree.st_uid != os.geteuid()
        ):
            raise RelationViolationError(f"Cutover owner {name!r} lost its saved session")
        participants.append(replace(thread, pid=0, active_turn=None))
    old_waits = GoalWaits(legacy.root / "goal_waits.json").read()
    migrated_waits = []
    seen_goals: set[str] = set()
    selected_owners = {participant.name: participant for participant in participants}
    for thread in participants:
        goal = thread.goal
        if goal is None or not goal.state.active:
            continue
        if goal.id in seen_goals:
            raise RelationViolationError("Cutover active goal identities collide")
        seen_goals.add(goal.id)
        wait = old_waits.get(goal.id)
        if wait is None:
            continue
        if (
            wait.owner_created_at not in (None, thread.created_at)
            or wait.revision > goal.revision
            or not wait.targets
        ):
            raise RelationViolationError("Cutover active goal wait lost its owner binding")
        if any(
            target.name not in selected_owners
            or selected_owners[target.name].created_at != target.created_at
            for target in wait.targets
        ):
            raise RelationViolationError("Cutover goal wait requires every exact target staged")
        # New wire sequences start at one. Preserve the dependency wait, but
        # only a FRESH private-root reply may release it. A legacy child-turn
        # callback cannot certify new-root completion after migration.
        migrated_waits.append(
            replace(
                wait,
                after_seq=0,
                owner_created_at=thread.created_at,
                target_turn_generations=tuple(None for _ in wait.targets),
                report_turn_id=None,
                report_turn_generation=None,
            )
        )
    if (
        (private.root / "bus_meta.json").exists()
        or (private.root / "bus.jsonl").exists()
        or private.registry.snapshot().threads
    ):
        raise RelationViolationError("Cutover participants require a fresh private root")
    root_id = private.messaging.initialize_private_initial_protocol()
    # A claim read barrier can only be installed while the private bus is
    # empty. Selected owner writes on this route need it before any USER row.
    private.messaging.initialize_private_claim_protocol()
    install_private_bus_checkpoint(private.bus)
    with _store_lock(private._wire_lock_path):
        new_waits = GoalWaits(private.root / "goal_waits.json")
        for wait in migrated_waits:
            new_waits.record(wait)
        for thread in participants:
            private.registry.register(thread, StoppedThreadStatus())
    # A staged owner must be ready for the first private USER row before the
    # default route is published. The foreground-only setup path normally
    # installs these schemas, but a saved-session cutover does not use it.
    with MutationStore(str(private.root / "coordination.sqlite3")) as store:
        install_private_cohort_schema(store)
        install_private_response_schema(store)
        install_native_runtime_schema(store)
        install_prompt_binding_schema(store)
        for thread in participants:
            store.register_participant(
                stable_thread_lookup(thread.created_at),
                thread.name,
                thread.name,
                committed=True,
            )
    _require_unchanged_archive_source(legacy, archive)
    return root_id, tuple(witnesses[name] for name in selected)


def activate_stopped_legacy_route(
    legacy: Comms,
    private: Comms,
    inventory: LegacyInventory,
    selected_names: Sequence[str],
    archive_destination: Path,
    native_package: Path,
    route_path: Path | None = None,
) -> tuple[ArchiveReceipt, ActiveRoute, tuple[OwnerWitness, ...]]:
    """Archive, stage, and publish under one default-route exclusive lock.

    The operator must already have fenced explicit-root ingress and Toad child
    process groups. This function never stops, starts, prompts, or replays an
    owner. On a failure it leaves the archive/root for inspection and does not
    retry publication.
    """
    from .active_route import (
        ActiveRoute,
        _publish_active_route_locked,
        active_route_path,
        read_active_route,
    )
    from .cohort_foreground import _preflight, _trusted_package

    route_path = active_route_path() if route_path is None else route_path
    if route_path.name != "active-route.json" or not route_path.is_absolute():
        raise ValueError("Cutover requires the absolute default route path")
    if legacy.root.resolve() != (Path.home() / ".agent-comms").resolve():
        raise ValueError("Cutover requires the local legacy default root")
    if (
        not private.root.is_absolute()
        or private.root == Path("/var/tmp")
        or not private.root.is_relative_to("/var/tmp")
        or ".." in private.root.parts
        or private.root.resolve() != private.root
        or not native_package.is_absolute()
        or ".." in native_package.parts
        or archive_destination.resolve().is_relative_to(private.root.resolve())
    ):
        raise ValueError("Cutover requires an absolute private root and Pi package")
    _trusted_package(native_package)
    route_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = os.open(
        route_path.parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        info = os.fstat(directory)
        parent = route_path.parent.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or (parent.st_dev, parent.st_ino) != (info.st_dev, info.st_ino)
        ):
            raise ValueError("Cutover route directory changed or is not owned")
        os.fchmod(directory, 0o700)
        if read_active_route(route_path) is not None:
            raise ValueError("Cutover route is already installed")
        archive = archive_stopped_root(legacy, archive_destination)
        root_id, witnesses = stage_private_participants(
            legacy, private, archive, inventory, selected_names
        )
        route = ActiveRoute(private.root, root_id, native_package)
        _preflight(route.root, route.wire_root_id, route.native_package, True)
        _publish_active_route_locked(route, route_path, directory)
        return archive, route, witnesses
    finally:
        os.close(directory)
