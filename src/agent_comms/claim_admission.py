"""Read-only verification of one selected wake against its durable owners.

This preflight does not grant a file write. The publication/write boundary must
repeat it while holding the wire, bus, and coordinator authority locks.
"""

from __future__ import annotations

from .bus_publication import stable_thread_lookup
from .coordination import (
    ACTIVE_ATTEMPT_PHASES,
    ClaimDisposition,
    ExecutionStatus,
    TriageVerdict,
    WakeMode,
)
from .coordination_cohort import _assert_schema, _receipt_matches
from .coordination_store import IdentityConflict, MutationStore
from .declarations import RelationViolationError, _store_lock
from .envelope_claim_transitions import WakeAdmission
from .operations import Comms


def verify_selected_wake(
    comms: Comms, store: MutationStore, admission: WakeAdmission, owner_name: str
) -> None:
    """Reject any unselected, stale, stopped, or unrelated N/K execution."""
    if type(comms) is not Comms or type(store) is not MutationStore:
        raise TypeError("Wake admission requires the actual wire and coordinator stores")
    if type(admission) is not WakeAdmission or type(owner_name) is not str:
        raise IdentityConflict("Wake admission is not typed")
    with _store_lock(comms._wire_lock_path):
        try:
            initial = comms.bus.read_initial_cohort(admission.wire_root_id, admission.source_seq)
            owner, generation = comms.registry.live_owner_with_admission(owner_name)
        except (RelationViolationError, ValueError) as error:
            raise IdentityConflict("Wake bus or live owner authority changed") from error
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
                or attempt.phase not in ACTIVE_ATTEMPT_PHASES
                or attempt.attempt_ordinal != admission.attempt_ordinal
                or attempt.owner_generation != admission.participant_generation
                or attempt.owner_thread != owner.name
                or claim.claim_id not in {row.claim_id for row in snapshot.claims}
            ):
                raise IdentityConflict("Wake execution is not the current selected attempt")
