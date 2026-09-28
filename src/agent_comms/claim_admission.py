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

from .bus_publication import CommittedInitial, stable_thread_lookup
from .channel_targets import BuiltinChannel
from .cohort_schema import assert_cohort_schema
from .comms import Comms
from .coordination_cohort import _receipt_matches
from .coordination_store import IdentityConflict, MutationStore
from .envelope_claim_transitions import (
    ClaimConflict,
    ClaimOwner,
    ExistingFileClaim,
    FileClaimPath,
    WakeAdmission,
)
from .errors import RelationViolationError
from .messages import Message, MessageType
from .private_registry_guard import _require_no_private_owner_rename
from .store_files import _store_lock
from .threads import Thread


def verify_selected_wake(
    comms: Comms, store: MutationStore, admission: WakeAdmission, owner_name: str
) -> None:
    """Reject any unselected, stale, stopped, or unrelated N/K execution."""
    if type(comms) is not Comms or type(store) is not MutationStore:
        raise TypeError("Wake admission requires the actual wire and coordinator stores")
    if type(admission) is not WakeAdmission or type(owner_name) is not str:
        raise IdentityConflict("Wake admission is not typed")
    if store.path.resolve() != (comms.root / "coordination.sqlite3").resolve():
        raise IdentityConflict("Wake coordinator does not belong to this wire root")
    with _store_lock(comms._wire_lock_path):
        _require_no_private_owner_rename(comms.root)
        try:
            with comms.bus.log.locked():
                marker = comms.bus.log._private_marker_unlocked()
                if admission.source_seq <= marker.admission_after_seq:
                    raise IdentityConflict("Selected wake precedes the current admission floor")
            initial = comms.bus.log.read_initial_cohort(
                admission.wire_root_id, admission.source_seq
            )
            owner, generation = comms.registry.live_owner_with_admission(owner_name)
        except (RelationViolationError, ValueError) as error:
            raise IdentityConflict("Wake bus or live owner authority changed") from error
        _verify_selected_wake_state(initial, owner, generation, store, admission)


def _verify_selected_wake_state(
    initial: CommittedInitial,
    owner: Thread,
    generation: int,
    store: MutationStore,
    admission: WakeAdmission,
) -> None:
    """Check an already locked bus/owner snapshot against one SQL state."""
    turn = owner.active_turn
    if (
        initial.message.message_id != admission.source_message_id
        or generation != admission.owner_admission_generation
        or stable_thread_lookup(owner.created_at) != admission.recipient_lookup
        or turn is None
        or turn.id != admission.turn_id
        or turn.admission_generation != generation
    ):
        raise IdentityConflict("Wake source or owner turn does not match")
    with store._read_transaction():
        assert_cohort_schema(store._connection)
        receipt = _receipt_matches(store._connection, initial)
        if not any(
            assignment.assignment_id == admission.wake_assignment_id
            for assignment in receipt.assignments
        ):
            raise IdentityConflict("Wake claim is not in the sealed selected cohort")
        assignment = store.assignment(admission.wake_assignment_id)
        participant = store.participant(admission.recipient_lookup)
        snapshot = store.snapshot(admission.execution_id)
        attempt = snapshot.attempt
        if (
            assignment.recipient_lookup != admission.recipient_lookup
            or assignment.recipient != owner.name
            or assignment.wire_seq != admission.source_seq
            or assignment.message_id != admission.source_message_id
            or assignment.revision != admission.wake_revision
            or assignment.lifecycle.execution_id != admission.execution_id
            or not assignment.lifecycle.engaged
            or not participant.committed
            or participant.owner_thread != owner.name
            or participant.participant_generation != admission.participant_generation
            or not snapshot.execution.lifecycle.active
            or snapshot.execution.owner_lookup != admission.recipient_lookup
            or snapshot.execution.owner_thread != owner.name
            or not snapshot.is_current
            or attempt is None
            or not attempt.lifecycle.allows_tool_admission
            or attempt.lifecycle.backend_done
            or attempt.lifecycle.process_dead
            or attempt.attempt_ordinal != admission.attempt_ordinal
            or attempt.owner_generation != admission.participant_generation
            or attempt.owner_thread != owner.name
            or assignment.assignment_id not in {row.assignment_id for row in snapshot.assignments}
        ):
            raise IdentityConflict("Wake execution is not the current selected attempt")


@contextmanager
def _selected_claim_boundary(
    comms: Comms, store: MutationStore, admission: WakeAdmission, owner_name: str
):
    """One wire/bus/registry boundary for claim acquisition and exact release."""
    if type(comms) is not Comms or type(store) is not MutationStore:
        raise TypeError("Wake claim requires the actual wire and coordinator stores")
    if type(admission) is not WakeAdmission or type(owner_name) is not str:
        raise IdentityConflict("Wake claim admission is not typed")
    if store.path.resolve() != (comms.root / "coordination.sqlite3").resolve():
        raise IdentityConflict("Wake coordinator does not belong to this wire root")
    bus = comms.bus
    with (
        _store_lock(comms._wire_lock_path),
        bus.log.locked(),
        _store_lock(comms.registry.store.path),
    ):
        registry = comms.registry.store._read_unlocked().snapshot()
        canonical = registry.aliases.get(owner_name, owner_name)
        owner = registry.threads.get(canonical)
        status = registry.statuses.get(canonical)
        generation = registry.admission_generations.get(canonical)
        if (
            owner is None
            or status is None
            or not status.active
            or owner.pid != os.getpid()
            or not owner.role.executable
            or generation is None
        ):
            raise IdentityConflict("Selected wake owner stopped or changed")
        _require_no_private_owner_rename(comms.root)
        metadata = bus.log._private_marker_unlocked()
        if metadata.root_id != admission.wire_root_id:
            raise IdentityConflict("Selected wake belongs to another wire root")
        if admission.source_seq <= metadata.admission_after_seq:
            raise IdentityConflict("Selected wake precedes the current admission floor")
        initial = next(
            (
                row
                for _message, _receipt, row in bus.log._verified_private_rows_unlocked(metadata)
                if row is not None and row.message.seq == admission.source_seq
            ),
            None,
        )
        if initial is None:
            raise IdentityConflict("Selected wake has no committed initial row")
        with store._read_transaction():
            _verify_selected_wake_state(initial, owner, generation, store, admission)
            yield bus, metadata, registry, owner, initial


def publish_selected_resource_claim(
    comms: Comms,
    store: MutationStore,
    admission: WakeAdmission,
    owner_name: str,
    resource_path: FileClaimPath,
) -> ClaimOwner:
    """Bind one existing file claim to a live selected wake in a durable bus row.

    The common lock order keeps stop and coordinator settlement behind the
    pre-append check. A lost append result is UNKNOWN; this call never retries it.
    The returned claim is an ownership receipt, not file-write authorization.
    """
    with _selected_claim_boundary(comms, store, admission, owner_name) as (
        bus,
        metadata,
        registry,
        owner,
        initial,
    ):
        resource = resource_path.normalized(Path(owner.worktree))
        projection, _ = bus.log._claim_projection_unlocked(metadata)
        existing = projection.get(resource)
        if existing is not None:
            if (
                existing.admission == admission
                and existing.incarnation == str(owner.created_at)
                and existing.owner == owner.name
            ):
                return existing
            raise ClaimConflict(existing)
        for prior, _receipt, _initial in bus.log._verified_private_rows_unlocked(metadata):
            transition = prior.claim_transition
            if (
                transition is not None
                and transition.admission is not None
                and transition.admission.operation_id == admission.operation_id
            ):
                raise IdentityConflict("Wake claim operation was already consumed")
        target = initial.message.sender
        if target == owner.name:
            target = BuiltinChannel.ALL.value
        committed = bus.publisher.publish_claim_envelope(
            Message(owner.name, target, "Resource claim admitted", MessageType.INFO, notice=True),
            worktree=Path(owner.worktree),
            incarnation=str(owner.created_at),
            claims=[resource_path],
            _locked_registry_snapshot=registry,
            _bus_locked=True,
            _admission=admission,
        )
        transition = committed.claim_transition
        assert transition is not None and transition.generation is not None
        return ClaimOwner(
            resource,
            transition.owner,
            transition.incarnation,
            transition.generation,
            committed.seq,
            committed.message_id,
            admission,
        )


def release_selected_resources(
    comms: Comms,
    store: MutationStore,
    admission: WakeAdmission,
    owner_name: str,
    claims: tuple[ClaimOwner, ...],
) -> None:
    """Release only these observed generations under their current selected owner."""
    with _selected_claim_boundary(comms, store, admission, owner_name) as (
        bus,
        metadata,
        registry,
        owner,
        initial,
    ):
        projection, _ = bus.log._claim_projection_unlocked(metadata)
        if any(projection.get(assignment.resource) != assignment for assignment in claims):
            raise IdentityConflict("Coding claim changed before release")
        if claims:
            target = (
                initial.message.sender
                if initial.message.sender != owner.name
                else BuiltinChannel.ALL.value
            )
            bus.publisher.publish_claim_envelope(
                Message(
                    owner.name,
                    target,
                    "Completed coding tool claims released",
                    MessageType.INFO,
                    notice=True,
                ),
                worktree=Path(owner.worktree),
                incarnation=str(owner.created_at),
                releases=tuple(assignment.resource for assignment in claims),
                _locked_registry_snapshot=registry,
                _bus_locked=True,
            )


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
    store: MutationStore,
    admission: WakeAdmission,
    owner_name: str,
    claimed: ClaimOwner,
    contents: bytes,
) -> None:
    """Mediate one bounded existing-file replacement under *current* authority.

    This explicitly invoked API is not an interceptor for Pi shell/edit tools,
    subprocesses, or human edits. Failure after truncation/write is UNKNOWN;
    callers must inspect, never automatically retry this operation.
    """
    if type(comms) is not Comms or type(store) is not MutationStore:
        raise TypeError("Selected write requires the actual wire and coordinator stores")
    if (
        type(admission) is not WakeAdmission
        or type(owner_name) is not str
        or type(claimed) is not ClaimOwner
        or type(contents) is not bytes
        or len(contents) > 1024 * 1024
    ):
        raise IdentityConflict("Selected write requires a bounded typed claim and bytes")
    if store.path.resolve() != (comms.root / "coordination.sqlite3").resolve():
        raise IdentityConflict("Selected write coordinator belongs to another root")
    if not hasattr(os, "O_NOFOLLOW"):
        raise IdentityConflict("Selected write requires no-follow file descriptors")
    bus = comms.bus
    with (
        _store_lock(comms._wire_lock_path),
        bus.log.locked(),
        _store_lock(comms.registry.store.path),
    ):
        registry = comms.registry.store._read_unlocked().snapshot()
        canonical = registry.aliases.get(owner_name, owner_name)
        owner = registry.threads.get(canonical)
        status = registry.statuses.get(canonical)
        generation = registry.admission_generations.get(canonical)
        if (
            owner is None
            or status is None
            or not status.active
            or owner.pid != os.getpid()
            or not owner.role.executable
            or generation is None
            or generation != admission.owner_admission_generation
        ):
            raise IdentityConflict("Selected write owner stopped or changed")
        _require_no_private_owner_rename(comms.root)
        marker = bus.log._private_marker_unlocked()
        if marker.root_id != admission.wire_root_id:
            raise IdentityConflict("Selected write belongs to another private root")
        initial = next(
            (
                row
                for _message, _receipt, row in bus.log._verified_private_rows_unlocked(marker)
                if row is not None and row.message.seq == admission.source_seq
            ),
            None,
        )
        if initial is None:
            raise IdentityConflict("Selected write source is not committed")
        # A separate fail-fast coordinator connection holds the transaction
        # through fsync. A concurrently settling attempt must not slip between
        # verification and irreversible file mutation.
        with MutationStore(str(store.path), lock_timeout=0) as scoped, scoped._transaction():
            _verify_selected_wake_state(initial, owner, generation, scoped, admission)
            normalized = ExistingFileClaim(Path(claimed.resource)).normalized(Path(owner.worktree))
            projection, _ = bus.log._claim_projection_unlocked(marker)
            if claimed.admission != admission or projection.get(normalized) != claimed:
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
