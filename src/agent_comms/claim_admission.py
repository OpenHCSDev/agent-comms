"""Verify a selected N/K wake and bind a durable resource claim to it.

Neither verification nor a committed claim grants a native file write. The
write boundary must repeat current authority checks before touching the file.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from .bus_publication import CommittedInitial, stable_thread_lookup
from .coordination import (
    AttemptPhase,
    ClaimDisposition,
    ExecutionStatus,
    TriageVerdict,
    WakeMode,
)
from .coordination_cohort import _assert_schema, _receipt_matches
from .coordination_store import IdentityConflict, MutationStore
from .declarations import (
    Message,
    MessageType,
    RelationViolationError,
    Thread,
    _store_lock,
)
from .envelope_claim_transitions import (
    ClaimConflict,
    ClaimOwner,
    WakeAdmission,
    normalize_existing_file,
)
from .operations import Comms


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
        try:
            initial = comms.bus.read_initial_cohort(admission.wire_root_id, admission.source_seq)
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
        _assert_schema(store._connection)
        receipt = _receipt_matches(store._connection, initial)
        if not any(claim.claim_id == admission.wake_claim_id for claim in receipt.claims):
            raise IdentityConflict("Wake claim is not in the sealed selected cohort")
        claim = store.claim(admission.wake_claim_id)
        participant = store.participant(admission.recipient_lookup)
        snapshot = store.snapshot(admission.execution_id)
        attempt = snapshot.attempt
        if (
            claim.recipient_lookup != admission.recipient_lookup
            or claim.recipient != owner.name
            or claim.wire_seq != admission.source_seq
            or claim.message_id != admission.source_message_id
            or claim.revision != admission.wake_revision
            or claim.execution_id != admission.execution_id
            or claim.disposition is not ClaimDisposition.ENGAGED
            or not (
                claim.wake_mode is WakeMode.FULL
                or (
                    claim.wake_mode is WakeMode.BOUNDED_TRIAGE
                    and claim.triage_verdict is TriageVerdict.ENGAGE
                )
            )
            or not participant.committed
            or participant.owner_thread != owner.name
            or participant.generation != admission.participant_generation
            or snapshot.execution.status is not ExecutionStatus.ACTIVE
            or snapshot.execution.owner_lookup != admission.recipient_lookup
            or snapshot.execution.owner_thread != owner.name
            or not snapshot.is_current
            or attempt is None
            or attempt.phase
            not in {
                AttemptPhase.PROMPT_STARTING,
                AttemptPhase.PROMPT_ACCEPTED,
                AttemptPhase.MODEL_RUNNING,
                AttemptPhase.TOOL_RUNNING,
            }
            or attempt.backend_done
            or attempt.process_dead
            or attempt.attempt_ordinal != admission.attempt_ordinal
            or attempt.owner_generation != admission.participant_generation
            or attempt.owner_thread != owner.name
            or claim.claim_id not in {row.claim_id for row in snapshot.claims}
        ):
            raise IdentityConflict("Wake execution is not the current selected attempt")


def publish_selected_resource_claim(
    comms: Comms,
    store: MutationStore,
    admission: WakeAdmission,
    owner_name: str,
    resource_path: str | Path,
) -> ClaimOwner:
    """Bind one existing file claim to a live selected wake in a durable bus row.

    The common lock order keeps stop and coordinator settlement behind the
    pre-append check. A lost append result is UNKNOWN; this call never retries it.
    The returned claim is an ownership receipt, not file-write authorization.
    """
    if type(comms) is not Comms or type(store) is not MutationStore:
        raise TypeError("Wake claim requires the actual wire and coordinator stores")
    if type(admission) is not WakeAdmission or type(owner_name) is not str:
        raise IdentityConflict("Wake claim admission is not typed")
    if store.path.resolve() != (comms.root / "coordination.sqlite3").resolve():
        raise IdentityConflict("Wake coordinator does not belong to this wire root")
    bus = comms.bus
    with (
        _store_lock(comms._wire_lock_path),
        _store_lock(bus._path),
        _store_lock(comms.registry._path),
    ):
        registry = comms.registry._snapshot_unlocked()
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
        metadata = bus._private_marker_unlocked()
        if metadata["wire_root_id"] != admission.wire_root_id:
            raise IdentityConflict("Selected wake belongs to another wire root")
        initial = next(
            (
                row
                for _message, _receipt, row in bus._verified_private_rows_unlocked(metadata)
                if row is not None and row.message.seq == admission.source_seq
            ),
            None,
        )
        if initial is None:
            raise IdentityConflict("Selected wake has no committed initial row")
        with store._read_transaction():
            _verify_selected_wake_state(initial, owner, generation, store, admission)
            resource = normalize_existing_file(Path(owner.worktree), resource_path)
            projection, _ = bus._claim_projection_unlocked(metadata)
            existing = projection.get(resource)
            if existing is not None:
                if (
                    existing.admission == admission
                    and existing.incarnation == str(owner.created_at)
                    and existing.owner == owner.name
                ):
                    return existing
                raise ClaimConflict(existing)
            for prior, _receipt, _initial in bus._verified_private_rows_unlocked(metadata):
                transition = prior.claim_transition
                if (
                    transition is not None
                    and transition.admission is not None
                    and transition.admission.operation_id == admission.operation_id
                ):
                    raise IdentityConflict("Wake claim operation was already consumed")
            target = initial.message.sender
            if target == owner.name:
                target = "#all"
            committed = bus.publish_claim_envelope(
                Message(
                    owner.name, target, "Resource claim admitted", MessageType.INFO, notice=True
                ),
                worktree=Path(owner.worktree),
                incarnation=str(owner.created_at),
                claims=[resource],
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
        _store_lock(bus._path),
        _store_lock(comms.registry._path),
    ):
        registry = comms.registry._snapshot_unlocked()
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
        marker = bus._private_marker_unlocked()
        if marker["wire_root_id"] != admission.wire_root_id:
            raise IdentityConflict("Selected write belongs to another private root")
        initial = next(
            (
                row
                for _message, _receipt, row in bus._verified_private_rows_unlocked(marker)
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
            normalized = normalize_existing_file(Path(owner.worktree), claimed.resource)
            projection, _ = bus._claim_projection_unlocked(marker)
            if claimed.admission != admission or projection.get(normalized) != claimed:
                raise IdentityConflict("Selected write has no current exact resource claim")
            before = os.stat(normalized, follow_symlinks=False)
            fd = os.open(normalized, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                opened = os.fstat(fd)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                ):
                    raise IdentityConflict("Selected write file changed before opening")
                os.ftruncate(fd, 0)
                view = memoryview(contents)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("Selected write made no progress; file outcome UNKNOWN")
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
