"""Verify a selected N/K wake and bind a durable resource claim to it.

Neither verification nor a committed claim grants a native file write. The
write boundary must repeat current authority checks before touching the file.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from agent_comms.coordination_errors import IdentityConflict
from agent_comms.coordinator import Coordination

from .bus_publication import CommittedDelivery
from .cohort_schema import assert_cohort_schema
from .comms import Comms
from .coordination_cohort import _claim_record
from .coordination_errors import StaleFence
from .coordination_tables.participants import OwnerGenerations
from .envelope_claim_transitions import (
    ExistingFileClaim,
    FileClaimPath,
    WakeClaim,
    WakeClaimOwner,
    WakeClaims,
)
from .errors import RelationViolationError
from .native_input_owner import RegistryOwner
from .private_registry_guard import _require_no_private_owner_rename
from .store_files import SharedLock, _store_lock
from .threads import Thread
from .wire_log import CheckedWireStoreLock


def verify_selected_wake(
    comms: Comms, store: Coordination, claim_scope: WakeClaim, owner_name: str
) -> None:
    """Reject any unselected, stale, stopped, or unrelated N/K execution."""
    if store.session.path.resolve() != (comms.root / "coordination.sqlite3").resolve():
        raise IdentityConflict("Wake coordinator does not belong to this wire root")
    with _store_lock(comms._wire_lock_path, mode=SharedLock()):
        _require_no_private_owner_rename(comms.root)
        try:
            with comms.bus.log.checked_read() as source:
                marker = source.marker
                if claim_scope.source_seq <= marker.claimed_after_seq:
                    raise IdentityConflict("Selected wake precedes the current claim_scope floor")
            initial = comms.bus.log.read_delivery_cohort(
                claim_scope.wire_root_id, claim_scope.source_seq
            )
            owner, generation = comms.registry.live_owner_with_claim(owner_name)
        except (RelationViolationError, ValueError) as error:
            raise IdentityConflict("Wake bus or live owner authority changed") from error
        _verify_selected_wake_state(initial, owner, generation, store, claim_scope)


def _verify_selected_wake_state(
    initial: CommittedDelivery,
    owner: Thread,
    generation: int,
    store: Coordination,
    claim_scope: WakeClaim,
) -> None:
    """Check an already locked bus/owner snapshot against one SQL state."""
    registry_owner = RegistryOwner(thread=owner, claimed_generation=generation)
    try:
        turn = registry_owner.require_active_turn()
    except StaleFence as error:
        raise IdentityConflict("Wake source or owner turn does not match") from error
    if initial.message.reference != claim_scope.source:
        raise IdentityConflict("Wake source message changed")
    if owner.incarnation.lookup != claim_scope.recipient_lookup:
        raise IdentityConflict("Wake recipient incarnation changed")
    if generation != claim_scope.owner_claimed_generation or turn.id != claim_scope.turn_id:
        raise IdentityConflict("Wake source or owner turn does not match")
    with store.session.read():
        assert_cohort_schema(store.session._connection)
        acknowledgement = _claim_record(store.session._connection, initial)
        if not any(
            assignment.assignment_id == claim_scope.wake_assignment_id
            for assignment in acknowledgement.assignments
        ):
            raise IdentityConflict("Wake claim is not in the sealed selected cohort")
        assignment = store.assignments.get(claim_scope.wake_assignment_id)
        participant = store.participants.get(claim_scope.recipient_lookup)
        snapshot = store.snapshots.get(claim_scope.execution_id)
        if assignment.require_engaged_binding() != claim_scope.binding:
            raise IdentityConflict("Wake assignment binding changed")
        expected_owner = OwnerGenerations(
            owner_lookup=claim_scope.recipient_lookup,
            owner_thread=owner.name,
            generation=claim_scope.participant_generation,
        )
        if not participant.committed or participant.owner_identity != expected_owner:
            raise IdentityConflict("Wake participant generation changed")
        try:
            attempt = snapshot.require_current_attempt()
        except StaleFence as error:
            raise IdentityConflict("Wake execution is not the current selected attempt") from error
        if (
            attempt.owner_identity != expected_owner
            or attempt.attempt_ordinal != claim_scope.attempt_ordinal
        ):
            raise IdentityConflict("Wake selected attempt owner changed")
        if snapshot.execution.owner_thread != owner.name:
            raise IdentityConflict("Wake execution owner changed")
        if not attempt.lifecycle.tool_claim_open:
            raise IdentityConflict("Wake execution is not the current selected attempt")
        if assignment not in snapshot.assignments or assignment.recipient != owner.name:
            raise IdentityConflict("Wake assignment is not a member of this owner's execution")


def _committed_source(
    bus_lock: CheckedWireStoreLock, claim_scope: WakeClaim, absent: str
) -> CommittedDelivery:
    """The message a selected turn answers, read through the held bus lock's checkpoint."""
    try:
        return bus_lock.checked_read.delivery(claim_scope.source_seq)
    except RelationViolationError as error:
        raise IdentityConflict(absent) from error


@contextmanager
def _selected_claim_boundary(
    comms: Comms, store: Coordination, claim_scope: WakeClaim, owner_name: str
):
    """One wire/bus/registry boundary for selected claim acquisition."""
    if store.session.path.resolve() != (comms.root / "coordination.sqlite3").resolve():
        raise IdentityConflict("Wake coordinator does not belong to this wire root")
    bus = comms.bus
    with (
        _store_lock(comms._wire_lock_path),
        bus.log.locked() as bus_lock,
        _store_lock(comms.registry.store.path),
    ):
        registry = comms.registry.store.read_snapshot_unlocked()
        canonical = registry.canonical_name(owner_name)
        try:
            captured = RegistryOwner.capture(
                registry, canonical, "Selected wake owner stopped or changed"
            )
            captured.require_active_turn()
        except StaleFence as error:
            raise IdentityConflict("Selected wake owner stopped or changed") from error
        owner, generation = captured.thread, captured.claimed_generation
        _require_no_private_owner_rename(comms.root)
        metadata = bus.log.private_marker_unlocked()
        if metadata.root_id != claim_scope.wire_root_id:
            raise IdentityConflict("Selected wake belongs to another wire root")
        if claim_scope.source_seq <= metadata.claimed_after_seq:
            raise IdentityConflict("Selected wake precedes the current claim_scope floor")
        initial = _committed_source(
            bus_lock, claim_scope, "Selected wake has no committed initial row"
        )
        with store.session.read():
            _verify_selected_wake_state(initial, owner, generation, store, claim_scope)
            yield bus, bus_lock, metadata, registry, owner


def publish_selected_resource_claim(
    comms: Comms,
    store: Coordination,
    claim_scope: WakeClaim,
    owner_name: str,
    resource_path: FileClaimPath,
) -> WakeClaimOwner:
    """Bind one existing file claim to a live selected wake in a durable bus row.

    The common lock order keeps stop and coordinator settlement behind the
    pre-append check. A lost append result is UNKNOWN; this call never retries it.
    The returned claim is an ownership acknowledgement, not file-write authorization.
    """
    with (
        comms.bus.opened_activity() as activity,
        _selected_claim_boundary(comms, store, claim_scope, owner_name) as (
            bus,
            bus_lock,
            metadata,
            registry,
            owner,
        ),
    ):
        resource = resource_path.normalized(Path(owner.worktree))
        with bus_lock.current_source(bus.log, metadata) as source:
            existing = source.claim(resource)
            if existing is not None:
                if WakeClaimOwner.selected(existing, claim_scope):
                    return existing
            if source.claim_operation_used(claim_scope.operation_id):
                raise IdentityConflict("Wake claim operation was already consumed")
        committed = bus.publisher._publish_selected_claim_unlocked(
            bus_lock,
            registry,
            owner,
            resource,
            claim_scope,
            activity=activity,
        )
        transition = committed.claim_transition
        assert isinstance(transition, WakeClaims)
        return transition.claimed(resource)


@contextmanager
def _opened_selected_file(
    worktree: Path, normalized: str
) -> Iterator[tuple[int, Callable[[], None]]]:
    """Anchor every parent component to physical worktree directory FDs.

    Path normalization alone is not enough: a parent can be replaced by a
    symlink before a final-component O_NOFOLLOW open. Preflight stability is
    required before truncation; post-fsync drift is UNKNOWN, not success.
    This does not promise hostile same-UID rename exclusion during a write.
    """
    relative = Path(normalized).relative_to(worktree)
    if not relative.parts or not hasattr(os, "O_DIRECTORY"):
        raise IdentityConflict("Selected write needs a physical directory chain")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    with ExitStack() as opened:
        try:
            root_fd = os.open(worktree, directory_flags)
            opened.callback(os.close, root_fd)
            chain: list[tuple[int | None, str | Path, int]] = [(None, worktree, root_fd)]
            parent_fd = root_fd
            for part in relative.parts[:-1]:
                next_fd = os.open(part, directory_flags, dir_fd=parent_fd)
                opened.callback(os.close, next_fd)
                chain.append((parent_fd, part, next_fd))
                parent_fd = next_fd
            leaf = relative.parts[-1]
            before = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            fd = os.open(leaf, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
            opened.callback(os.close, fd)
            actual = os.fstat(fd)
            if (
                not stat.S_ISREG(actual.st_mode)
                or actual.st_nlink != 1
                or (actual.st_dev, actual.st_ino) != (before.st_dev, before.st_ino)
            ):
                raise IdentityConflict("Selected write file changed before opening")

            def stable() -> None:
                for parent, name, directory_fd in chain:
                    visible = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    held = os.fstat(directory_fd)
                    if not stat.S_ISDIR(visible.st_mode) or (visible.st_dev, visible.st_ino) != (
                        held.st_dev,
                        held.st_ino,
                    ):
                        raise IdentityConflict("Selected write parent directory changed")
                visible_file = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
                held_file = os.fstat(fd)
                if (
                    not stat.S_ISREG(visible_file.st_mode)
                    or held_file.st_nlink != 1
                    or (visible_file.st_dev, visible_file.st_ino)
                    != (held_file.st_dev, held_file.st_ino)
                ):
                    raise IdentityConflict("Selected write opened file changed")

            stable()
        except OSError as error:
            raise IdentityConflict("Selected write directory or file changed") from error
        yield fd, stable


def write_selected_claimed_file(
    comms: Comms,
    store: Coordination,
    claim_scope: WakeClaim,
    owner_name: str,
    claimed: WakeClaimOwner,
    contents: bytes,
) -> None:
    """Mediate one bounded existing-file replacement under *current* authority.

    This explicitly invoked API is not an interceptor for Pi shell/edit tools,
    subprocesses, or human edits. Failure after truncation/write is UNKNOWN;
    callers must inspect, never automatically retry this operation.
    """
    if len(contents) > 1024 * 1024:
        raise IdentityConflict("Selected write requires bounded bytes")
    if store.session.path.resolve() != (comms.root / "coordination.sqlite3").resolve():
        raise IdentityConflict("Selected write coordinator belongs to another root")
    if not hasattr(os, "O_NOFOLLOW"):
        raise IdentityConflict("Selected write requires no-follow file descriptors")
    bus = comms.bus
    with (
        _store_lock(comms._wire_lock_path),
        bus.log.locked() as bus_lock,
        _store_lock(comms.registry.store.path),
    ):
        registry = comms.registry.store.read_snapshot_unlocked()
        canonical = registry.canonical_name(owner_name)
        try:
            captured = RegistryOwner.capture(
                registry, canonical, "Selected write owner stopped or changed"
            )
            captured.require_active_turn()
        except StaleFence as error:
            raise IdentityConflict("Selected write owner stopped or changed") from error
        owner, generation = captured.thread, captured.claimed_generation
        _require_no_private_owner_rename(comms.root)
        marker = bus.log.private_marker_unlocked()
        if marker.root_id != claim_scope.wire_root_id:
            raise IdentityConflict("Selected write belongs to another private root")
        initial = _committed_source(bus_lock, claim_scope, "Selected write source is not committed")
        # The coordinator connection holds the transaction
        # through fsync. A concurrently settling attempt must not slip between
        # verification and irreversible file mutation.
        with Coordination(str(store.session.path)) as scoped, scoped.session.transaction():
            _verify_selected_wake_state(initial, owner, generation, scoped, claim_scope)
            normalized = ExistingFileClaim(Path(claimed.resource)).normalized(Path(owner.worktree))
            with bus_lock.current_source(bus.log, marker) as source:
                if claimed.claim_scope != claim_scope or source.claim(normalized) != claimed:
                    raise IdentityConflict("Selected write has no current exact resource claim")
            with _opened_selected_file(Path(owner.worktree), normalized) as (fd, stable):
                os.ftruncate(fd, 0)
                view = memoryview(contents)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("Selected write made no progress; file outcome UNKNOWN")
                    view = view[written:]
                os.fsync(fd)
                try:
                    stable()
                except (IdentityConflict, OSError) as error:
                    raise OSError(
                        "Selected write parent/file drift after fsync; outcome UNKNOWN"
                    ) from error
