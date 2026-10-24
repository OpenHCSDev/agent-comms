"""Slice-2 nonpublication mutation gates against the frozen schema."""

from __future__ import annotations

import hashlib
import multiprocessing
import sqlite3
import threading
from collections.abc import Callable
from multiprocessing.queues import Queue
from multiprocessing.synchronize import Event
from pathlib import Path

import pytest

from agent_comms.assignment_states import (
    AssignmentState,
    DeferredAssignment,
    EngagedAssignment,
    FailedAssignment,
    FullPendingAssignment,
    IgnoredAssignment,
    PassiveAssignment,
    TriagePendingAssignment,
)
from agent_comms.attempt_start import INITIAL_LEASE_DURATION_MS, AttemptStart
from agent_comms.attempt_states import (
    AttemptFailedAttempt,
    LiveAttempt,
    ModelRunningAttempt,
    PromptAcceptedAttempt,
    PromptStartingAttempt,
    SettlingAttempt,
    SucceededAttempt,
)
from agent_comms.coordination_errors import (
    IdentityConflict,
    IntegrityViolationError,
    PublicationUncertain,
    RecoveryBlocked,
    StaleFence,
    StaleRevision,
)
from agent_comms.coordination_results import AlreadyApplied, Applied
from agent_comms.coordination_snapshot import RecoverySnapshot
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.coordination_tables.attempts import ReplayFact
from agent_comms.coordination_tables.executions import (
    AcpOrigin,
    ExecutionProvenance,
    GoalOrigin,
    NoReplayAssessment,
    ReplayAssessment,
    ReplayAssessments,
    WireOrigin,
)
from agent_comms.coordination_tables.publications import (
    PublicationIntents,
)
from agent_comms.coordinator import StandaloneCoordination
from agent_comms.execution_states import (
    ActiveExecution,
    CompletedExecution,
    DeferredExecution,
    FailedExecution,
    PendingExecution,
)
from agent_comms.field_codec import FieldCodec, MessageId
from agent_comms.messages import InfoKind, Message
from agent_comms.owner_fence import OwnerFence, prepare_fence_token
from agent_comms.reason_codes import GivenReasonCode, NoDetail
from agent_comms.recovery_states import FailedRecovery
from agent_comms.response_acknowledgement import canonical_publication_key
from agent_comms.selected_source_batch import SelectedSource
from agent_comms.turn_failure import ProviderRetryExhausted
from agent_comms.wake_policy import (
    BoundedTriageWake,
    FullWake,
    MessageAudience,
    PassiveWake,
    WakePolicy,
)
from bus_record_fixture import OWNER_LOOKUP, RECIPIENT_LOOKUP, SENDER_LOOKUP, UNRELATED_LOOKUP


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "coordination.sqlite3"


def store(path: Path) -> StandaloneCoordination:
    return StandaloneCoordination(str(path), clock_ms=lambda: 1_000)


def wire_source(db: StandaloneCoordination, assignment_id: str, original: Message, mode: WakePolicy) -> SelectedSource:
    from agent_comms.audience_manifest import FrozenRecipient, freeze_audience
    from agent_comms.bus import PrivateRecord
    from agent_comms.selected_source_batch import SelectedSource
    from agent_comms.wake_policy import WakeDecision

    audience = freeze_audience(original, (FrozenRecipient(SENDER_LOOKUP, "Owner"),),
                               "0" * 64, sender_lookup=OWNER_LOOKUP,
                               sender_name=original.sender)
    decisions = (WakeDecision(SENDER_LOOKUP, MessageAudience.DIRECT, mode),)
    delivery = PrivateRecord.from_message(
        audience, decisions=decisions, control="ordinary")
    return SelectedSource(assignment_id, db.assignments, delivery)


def ready(
    path: Path, *, origin: type[ExecutionProvenance] = WireOrigin, max_attempts: int = 2
) -> StandaloneCoordination:
    db = store(path)
    sources: tuple[SelectedSource, ...] = ()
    assert isinstance(db.participants.register(SENDER_LOOKUP, committed=True), Applied)
    if origin is WireOrigin:
        original = Message("owner", "Owner", "Original request", InfoKind, timestamp=1.0, seq=1)
        db.assignments.accept(
            WakeAssignment(
                assignment_id="claim",
                recipient="Owner",
                recipient_lookup=SENDER_LOOKUP,
                wire_seq=1,
                message_id=original.message_id,
                audience=MessageAudience.DIRECT,
                accepted_at_ms=1_000,
                updated_at_ms=1_000,
                lifecycle=FullPendingAssignment.load(FullWake(), None, None, None),
            )
        )
        sources = (wire_source(db, "claim", original, FullWake()),)
    db.create(
        "exec",
        origin,
        SENDER_LOOKUP,
        max_attempts,
        sources=sources,
    )
    db.mark_pending("exec", expected_revision=1)
    return db


def started(db: StandaloneCoordination) -> tuple[str, OwnerFence]:
    token = prepare_fence_token()
    result = db.start(
        AttemptStart(
            "exec",
            1,
            SENDER_LOOKUP,
            1,
            token,
            expected_execution_revision=2,
            expected_pointer_revision=0,
        )
    )
    assert isinstance(result, Applied)
    return token, result.value.fence


def final_evidence(db: StandaloneCoordination, fence: OwnerFence, pointer_revision: int = 1) -> OwnerFence:
    from agent_comms.owner_fence import OwnerFence

    assert isinstance(fence, OwnerFence)
    result = db.advance(
        fence, PromptAcceptedAttempt, expected_pointer_revision=pointer_revision
    )
    result = db.advance(
        result.value.fence, ModelRunningAttempt, expected_pointer_revision=pointer_revision
    )
    result = db.advance(
        result.value.fence,
        SettlingAttempt,
        expected_pointer_revision=pointer_revision,
        backend_done=True,
        process_dead=True,
    )
    return result.value.fence


def _concurrent_registration(
    path: str, gate: Event, output: Queue[str]
) -> None:
    try:
        with StandaloneCoordination(path, clock_ms=lambda: 1_000) as db:
            gate.wait(timeout=5)
            result = db.participants.register(SENDER_LOOKUP, committed=True)
            output.put(type(result).__name__)
    except Exception as exc:
        output.put(type(exc).__name__)


def _concurrent_start(
    path: str, token: str, gate: Event, output: Queue[str]
) -> None:
    try:
        with StandaloneCoordination(path, clock_ms=lambda: 1_000) as db:
            gate.wait(timeout=5)
            result = db.start(
                AttemptStart(
                    "exec",
                    1,
                    SENDER_LOOKUP,
                    1,
                    token,
                    expected_execution_revision=2,
                    expected_pointer_revision=0,
                )
            )
            output.put(type(result).__name__)
    except Exception as exc:
        output.put(type(exc).__name__)


def test_registration_complete_atomic_reopen_and_generation(db_path: Path) -> None:
    db = store(db_path)
    original = db.participants.register(SENDER_LOOKUP, committed=False)
    assert isinstance(original, Applied)
    assert original.value.pointer.pointer_revision == 0
    assert isinstance(db.participants.register(SENDER_LOOKUP), AlreadyApplied)
    assert isinstance(db.participants.commit(SENDER_LOOKUP), Applied)
    assert isinstance(db.participants.commit(SENDER_LOOKUP), AlreadyApplied)
    assert isinstance(
        db.participants.advance_generation(SENDER_LOOKUP, expected_generation=1), Applied
    )
    with pytest.raises(StaleRevision):
        db.participants.advance_generation(SENDER_LOOKUP, expected_generation=1)
    db.close()
    with store(db_path) as reopened:
        assert reopened.participants.get(SENDER_LOOKUP).owner_identity.generation == 2
        assert reopened.participants.get(SENDER_LOOKUP).committed
        replay = reopened.participants.register(SENDER_LOOKUP, committed=False)
        assert isinstance(replay, AlreadyApplied)
        assert replay.value.owner_identity.generation == 2
        assert replay.value.owner_identity.owner_lookup == SENDER_LOOKUP


def test_claim_execution_idempotency_and_rollback(db_path: Path) -> None:
    db = ready(db_path)
    snap = db.get("exec")
    assert type(snap.execution.lifecycle) is PendingExecution
    assert type(snap.assignments[0].lifecycle) is EngagedAssignment
    assert snap.links[0].ordinal == 0
    assert len(snap.obligations) == 1
    original = Message("owner", "Owner", "Original request", InfoKind, timestamp=1.0, seq=1)
    replay = db.create(
        "exec",
        WireOrigin,
        SENDER_LOOKUP,
        2,
        sources=(wire_source(db, "claim", original, FullWake()),),
    )
    assert isinstance(replay, AlreadyApplied)
    different = Message("different", "Owner", "Original request", InfoKind,
                        timestamp=1.0, seq=1)
    with pytest.raises(IdentityConflict):
        db.create(
            "exec",
            WireOrigin,
            SENDER_LOOKUP,
            2,
            sources=(wire_source(db, "claim", different, FullWake()),),
        )
    db.close()
    with store(db_path) as reopened:
        assert reopened.get("exec").links == snap.links


def test_claim_acceptance_requires_initial_mode_decision(db_path: Path) -> None:
    with store(db_path) as db:
        db.participants.register(SENDER_LOOKUP, committed=True)

        def assignment(
            index: int,
            mode: WakePolicy,
            disposition: AssignmentState,
            verdict: str | None = None,
        ) -> WakeAssignment:
            return WakeAssignment(
                assignment_id=f"claim-{index}",
                recipient="Owner",
                recipient_lookup=SENDER_LOOKUP,
                wire_seq=index,
                message_id=MessageId.mint(f"message-{index}"),
                audience=MessageAudience.DIRECT,
                accepted_at_ms=1_000,
                updated_at_ms=1_000,
                lifecycle=disposition.load(mode, verdict, None, None),
            )

        for index, mode, disposition, verdict in (
            (1, BoundedTriageWake(), IgnoredAssignment, "ignore"),
            (2, BoundedTriageWake(), DeferredAssignment, None),
            (3, BoundedTriageWake(), FailedAssignment, None),
            (4, FullWake(), DeferredAssignment, None),
            (5, FullWake(), FailedAssignment, None),
        ):
            with pytest.raises(IdentityConflict):
                db.assignments.accept(assignment(index, mode, disposition, verdict))
            assert (
                db.session._connection.execute("SELECT count(*) FROM wake_claims").fetchone()[0]
                == 0
            )
        for index, initial_mode, initial_disposition in (
            (6, PassiveWake(), PassiveAssignment),
            (7, BoundedTriageWake(), TriagePendingAssignment),
            (8, FullWake(), FullPendingAssignment),
            (9, FullWake(), FullPendingAssignment),
        ):
            initial = assignment(index, initial_mode, initial_disposition)
            assert isinstance(db.assignments.accept(initial), Applied)
            assert db.assignments.get(initial.assignment_id).revision == 1
        ignored = db.assignments.transition_preengagement(
            "claim-7",
            IgnoredAssignment,
            expected_revision=1,
        )
        deferred = db.assignments.transition_preengagement(
            "claim-8", DeferredAssignment, expected_revision=1
        )
        failed = db.assignments.transition_preengagement(
            "claim-9", FailedAssignment, expected_revision=1
        )
        assert ignored.value.revision == deferred.value.revision == failed.value.revision == 2
        assert isinstance(
            db.assignments.accept(assignment(7, BoundedTriageWake(), TriagePendingAssignment)),
            AlreadyApplied,
        )
        assert type(db.assignments.get("claim-7").lifecycle) is IgnoredAssignment


def test_claim_preengagement_and_mixed_engagement_rollback(db_path: Path) -> None:
    with store(db_path) as db:
        db.participants.register(SENDER_LOOKUP, committed=True)
        sources = []
        for index, disposition in (
            (1, TriagePendingAssignment),
            (2, PassiveAssignment),
        ):
            mode = BoundedTriageWake() if index == 1 else PassiveWake()
            original = Message("owner", "Owner", f"Original request {index}", InfoKind,
                               timestamp=float(index), seq=index)
            db.assignments.accept(
                WakeAssignment(
                    assignment_id=str(index),
                    recipient="Owner",
                    recipient_lookup=SENDER_LOOKUP,
                    wire_seq=index,
                    message_id=original.message_id,
                    audience=MessageAudience.DIRECT,
                    accepted_at_ms=1_000,
                    updated_at_ms=1_000,
                    lifecycle=disposition.load(mode, None, None, None),
                )
            )
            sources.append(wire_source(db, str(index), original, mode))
        deferred = db.assignments.transition_preengagement(
            "1", DeferredAssignment, expected_revision=1
        )
        assert type(deferred.value.lifecycle) is DeferredAssignment
        with pytest.raises(StaleRevision):
            db.assignments.transition_preengagement("1", FailedAssignment, expected_revision=1)
        with pytest.raises((IdentityConflict, sqlite3.IntegrityError)):
            db.create(
                "exec",
                WireOrigin,
                SENDER_LOOKUP,
                2,
                sources=tuple(sources),
            )
        assert db.assignments.get("1").lifecycle.execution_id is None
        assert db.session._connection.execute("SELECT count(*) FROM executions").fetchone()[0] == 0
        assert not db.session._connection.in_transaction


def test_token_prepared_before_transaction_and_loss_replay_ignores_mutable(db_path: Path) -> None:
    with ready(db_path) as db:
        token = prepare_fence_token()
        assert len(token) == 64
        assert not db.session._connection.in_transaction
        first = db.start(
            AttemptStart(
                "exec",
                1,
                SENDER_LOOKUP,
                1,
                token,
                expected_execution_revision=2,
                expected_pointer_revision=0,
            )
        )
        assert isinstance(first, Applied)
        attempt = first.value.snapshot.attempt
        assert attempt is not None
        assert attempt.owner_token_digest == hashlib.sha256(token.encode()).hexdigest()
        assert isinstance(attempt.lifecycle, LiveAttempt)
        assert (
            attempt.lifecycle.lease_expires_at_ms
            == attempt.created_at_ms + INITIAL_LEASE_DURATION_MS
        )
        assert token not in str(FieldCodec.project(first.value.snapshot, "snapshot"))
        assert token not in db_path.read_bytes().decode("latin1")
        duplicate = db.start(
            AttemptStart(
                "exec",
                1,
                SENDER_LOOKUP,
                1,
                token,
                expected_execution_revision=2,
                expected_pointer_revision=0,
            )
        )
        assert isinstance(duplicate, AlreadyApplied)
        progressed = db.advance(
            first.value.fence, PromptAcceptedAttempt, expected_pointer_revision=1
        )
        duplicate = db.start(
            AttemptStart(
                "exec",
                1,
                SENDER_LOOKUP,
                1,
                token,
                expected_execution_revision=2,
                expected_pointer_revision=0,
            )
        )
        assert isinstance(duplicate, AlreadyApplied)
        assert duplicate.value.snapshot.attempt == progressed.value.snapshot.attempt
        with pytest.raises(IdentityConflict):
            db.start(
                AttemptStart(
                    "exec",
                    2,
                    SENDER_LOOKUP,
                    1,
                    token,
                    expected_execution_revision=2,
                    expected_pointer_revision=0,
                )
            )
        with pytest.raises(StaleRevision):
            db.advance(first.value.fence, ModelRunningAttempt, expected_pointer_revision=1)
    with store(db_path) as reopened:
        duplicate = reopened.start(
            AttemptStart(
                "exec",
                1,
                SENDER_LOOKUP,
                1,
                token,
                expected_execution_revision=2,
                expected_pointer_revision=0,
            )
        )
        assert isinstance(duplicate, AlreadyApplied)


def test_token_digest_is_global_and_only_digest_persists(db_path: Path) -> None:
    with ready(db_path) as db:
        token, _ = started(db)
        db.participants.register(RECIPIENT_LOOKUP, committed=True)
        db.create("other-exec", GoalOrigin, RECIPIENT_LOOKUP, 1)
        db.mark_pending("other-exec", expected_revision=1)
        with pytest.raises(IdentityConflict):
            db.start(
                AttemptStart(
                    "other-exec",
                    1,
                    RECIPIENT_LOOKUP,
                    1,
                    token,
                    expected_execution_revision=2,
                    expected_pointer_revision=0,
                )
            )
        assert db.get("other-exec").attempt is None
        assert (
            db.session._connection.execute("SELECT owner_token_digest FROM attempts").fetchone()[0]
            == hashlib.sha256(token.encode()).hexdigest()
        )


def test_settled_snapshot_encodes_recovery_kind_separately_from_table(db_path: Path) -> None:
    from agent_comms.coordination_tables.recovery import RecoveryAudit

    with store(db_path) as db:
        db.participants.register(SENDER_LOOKUP, committed=True)
        db.create("exec", AcpOrigin, SENDER_LOOKUP, 1)
        db.mark_pending("exec", expected_revision=1)
        _, fence = started(db)
        fence = final_evidence(db, fence)
        settled = db.settle_nonpublication(
            fence, expected_pointer_revision=1, outcome=AttemptFailedAttempt(),
            reason_code=GivenReasonCode("failed"))
        # Populate the original audit table for serialization, not owner-loss proof.
        with db.session.writing() as connection:
            RecoveryAudit(execution_id="exec", kind=FailedRecovery, reason_code=GivenReasonCode("failed"),
                sanitized_detail=NoDetail(), attempt=1, elapsed_ms=0, observed_at_ms=1_000).insert(connection)
        observed = db.get("exec")
        assert observed.execution == settled.value.execution
        encoded = FieldCodec.encode(observed)
        assert isinstance(encoded, dict)
        recovery = encoded["last_recovery"]
        assert isinstance(recovery, dict)
        assert recovery["row"] == RecoveryAudit.declared_name
        assert recovery["kind"] == FailedRecovery.declared_name
        assert FieldCodec.decode(RecoverySnapshot, encoded) == observed
        assert FieldCodec.decode(RecoveryAudit, encoded["last_recovery"]) == observed.last_recovery
        assert not observed.is_current and not observed.can_retry


def test_nonpublication_claimless_atomic_settlement(db_path: Path) -> None:
    with ready(db_path, origin=AcpOrigin) as db:
        _, fence = started(db)
        fence = final_evidence(db, fence)
        settled = db.settle_nonpublication(
            fence, expected_pointer_revision=1, outcome=SucceededAttempt()
        )
        assert type(settled.value.execution.lifecycle) is CompletedExecution
        assert settled.value.attempt is not None
        assert type(settled.value.attempt.lifecycle) is SucceededAttempt
        assert settled.value.obligations == ()
        assert settled.value.assignments == ()
        assert not settled.value.is_current
        assert settled.value.pointer_revision == 2
    with store(db_path) as reopened:
        assert type(reopened.get("exec").execution.lifecycle) is CompletedExecution


def test_retry_partition_and_generation_fence(db_path: Path) -> None:
    with ready(db_path) as db:
        token, fence = started(db)
        with db.session.writing() as connection:
            ReplayAssessments.authorize_if_unobserved(connection, "exec")
        fence = final_evidence(db, fence)
        outcome = db.settle_nonpublication(
            fence,
            expected_pointer_revision=1,
            outcome=AttemptFailedAttempt(),
            reason_code=GivenReasonCode("provider_failed"),
        )
        assert type(outcome.value.execution.lifecycle) is DeferredExecution
        assert outcome.value.can_retry
        with pytest.raises(StaleFence):
            db.start(
                AttemptStart(
                    "exec",
                    2,
                    SENDER_LOOKUP,
                    1,
                    prepare_fence_token(),
                    expected_execution_revision=4,
                    expected_pointer_revision=2,
                )
            )
        db.participants.advance_generation(SENDER_LOOKUP, expected_generation=1)
        second = db.start(
            AttemptStart(
                "exec",
                2,
                SENDER_LOOKUP,
                2,
                prepare_fence_token(),
                expected_execution_revision=4,
                expected_pointer_revision=2,
            )
        )
        assert second.value.snapshot.attempt is not None
        assert second.value.snapshot.attempt.attempt_ordinal == 2
        with pytest.raises(StaleFence):
            db.advance(fence, ModelRunningAttempt, expected_pointer_revision=3)
        assert isinstance(
            db.start(
                AttemptStart(
                    "exec",
                    1,
                    SENDER_LOOKUP,
                    1,
                    token,
                    expected_execution_revision=2,
                    expected_pointer_revision=0,
                )
            ),
            AlreadyApplied,
        )


def test_provider_failure_after_tool_start_is_terminal(db_path: Path) -> None:
    with ready(db_path, max_attempts=3) as db:
        _, fence = started(db)
        with db.session.writing() as connection:
            ReplayAssessments.accumulate(
                connection, "exec", ReplayFact.TOOL_CALL_STARTED
            )
        fence = final_evidence(db, fence)
        outcome = db.settle_nonpublication(
            fence,
            expected_pointer_revision=1,
            outcome=AttemptFailedAttempt(),
            reason_code=GivenReasonCode("provider_failed"),
            failure=ProviderRetryExhausted("controlled provider failure"),
        )
        assert type(outcome.value.execution.lifecycle) is FailedExecution
        assert not outcome.value.can_retry
        replay = outcome.value.replay.require_assessment()
        assert replay.facts == ReplayFact.TOOL_CALL_STARTED
        assert replay.side_effects_possible


@pytest.mark.parametrize("first_fact", ["process_dead", "backend_done"])
def test_final_backend_fact_blocks_later_phase_progress_and_dead_lease(
    db_path: Path, first_fact: str
) -> None:
    with ready(db_path) as db:
        _, fence = started(db)
        first = db.advance(
            fence,
            PromptStartingAttempt,
            expected_pointer_revision=1,
            backend_done=first_fact == "backend_done",
            process_dead=first_fact == "process_dead",
        )
        fence = first.value.fence
        before = db.get("exec")
        for phase, progress in (
            (PromptAcceptedAttempt, False),
            (PromptStartingAttempt, True),
            (PromptStartingAttempt, False),
        ):
            with pytest.raises(RecoveryBlocked):
                db.advance(fence, phase, expected_pointer_revision=1, progress=progress)
            assert db.get("exec") == before
        second_fact = "backend_done" if first_fact == "process_dead" else "process_dead"
        final = db.advance(
            fence,
            PromptStartingAttempt,
            expected_pointer_revision=1,
            backend_done=second_fact == "backend_done",
            process_dead=second_fact == "process_dead",
        )
        fence = final.value.fence
        assert final.value.snapshot.attempt is not None
        assert isinstance(final.value.snapshot.attempt.lifecycle, LiveAttempt)
        assert final.value.snapshot.attempt.lifecycle.backend_done
        assert final.value.snapshot.attempt.lifecycle.process_dead
        with pytest.raises(RecoveryBlocked):
            db.advance(fence, PromptStartingAttempt, expected_pointer_revision=1)
        outcome = db.settle_nonpublication(
            fence, expected_pointer_revision=1, outcome=AttemptFailedAttempt(), reason_code=GivenReasonCode("failed")
        )
        assert type(outcome.value.execution.lifecycle) is FailedExecution


@pytest.mark.parametrize(
    "fact", [ReplayFact.ASSISTANT_OUTPUT, ReplayFact.TOOL_CALL_STARTED]
)
def test_provider_proof_carries_until_later_attempt_records_output(
    db_path: Path, fact: ReplayFact
) -> None:
    with ready(db_path, max_attempts=3) as db:
        _, first = started(db)
        first = final_evidence(db, first)
        deferred = db.settle_nonpublication(
            first,
            expected_pointer_revision=1,
            outcome=AttemptFailedAttempt(),
            reason_code=GivenReasonCode("provider_failed"),
            failure=ProviderRetryExhausted("controlled provider failure"),
        )
        proof = deferred.value.replay.require_assessment()
        assert deferred.value.can_retry and proof.allows_retry
        db.participants.advance_generation(SENDER_LOOKUP, expected_generation=1)
        second = db.start(
            AttemptStart(
                "exec",
                2,
                SENDER_LOOKUP,
                2,
                prepare_fence_token(),
                expected_execution_revision=4,
                expected_pointer_revision=2,
            )
        )
        assert second.value.snapshot.replay == proof
        with db.session.writing() as connection:
            ReplayAssessments.accumulate(connection, "exec", fact)
        revoked = db.get("exec").replay.require_assessment()
        assert revoked.facts == fact and not revoked.allows_retry

        second_fence = final_evidence(db, second.value.fence, pointer_revision=3)
        outcome = db.settle_nonpublication(
            second_fence,
            expected_pointer_revision=3,
            outcome=AttemptFailedAttempt(),
            reason_code=GivenReasonCode("provider_failed_after_output"),
            failure=ProviderRetryExhausted("controlled provider failure"),
        )
        assert type(outcome.value.execution.lifecycle) is FailedExecution
        assert not outcome.value.can_retry
        assert outcome.value.replay == revoked


def test_replay_states_own_presence_revision_and_retry_authorization(db_path: Path) -> None:
    with ready(db_path) as store, store.session.writing() as connection:
        absent = store.get("exec").replay
        assert absent == NoReplayAssessment()
        assert FieldCodec.encode(absent, ReplayAssessment) is None
        assert FieldCodec.decode(ReplayAssessment, None) == absent
        snapshot = store.get("exec")
        encoded = FieldCodec.encode(snapshot)
        assert isinstance(encoded, dict)
        assert encoded["replay"] is None
        assert FieldCodec.decode(RecoverySnapshot, encoded) == snapshot
        absent.require_revision(None)
        assert not absent.allows_retry
        with pytest.raises(IntegrityViolationError):
            absent.require_assessment()
        with pytest.raises(StaleRevision):
            absent.require_revision(1)
        absent.revoke_retry(connection)
        assert ReplayAssessments.count(connection) == 0

        absent.authorize_retry(connection, "exec")
        recorded = ReplayAssessments.for_execution(connection, "exec").require_assessment()
        assert recorded.allows_retry and recorded.revision == 1
        assert FieldCodec.decode(ReplayAssessment, FieldCodec.encode(recorded)) == recorded
        recorded.require_revision(1)
        with pytest.raises(StaleRevision):
            recorded.require_revision(None)
        with pytest.raises(IntegrityViolationError):
            recorded.require_execution("another-execution")
        changes = connection.total_changes
        ReplayAssessments.authorize_if_unobserved(connection, "exec")
        assert not recorded.observe(
            connection, "exec", facts=ReplayFact.NONE,
            replay_safe=True, side_effects_possible=False,
        )
        assert connection.total_changes == changes
        ReplayAssessments.revoke(connection, "exec")
        revoked = ReplayAssessments.for_execution(connection, "exec").require_assessment()
        assert not revoked.allows_retry and revoked.revision == 2
        changes = connection.total_changes
        ReplayAssessments.revoke(connection, "exec")
        assert connection.total_changes == changes


def test_repeated_replay_fact_does_not_rewrite_its_durable_assessment(db_path: Path) -> None:
    with ready(db_path) as db:
        connection = db.session._connection
        ReplayAssessments.accumulate(connection, "exec", ReplayFact.ASSISTANT_OUTPUT)
        before = ReplayAssessments.for_execution(connection, "exec").require_assessment()
        changes = connection.total_changes
        for _ in range(100):
            ReplayAssessments.accumulate(connection, "exec", ReplayFact.ASSISTANT_OUTPUT)
        assert connection.total_changes == changes
        assert ReplayAssessments.for_execution(connection, "exec") == before
        ReplayAssessments.accumulate(connection, "exec", ReplayFact.THINKING_OUTPUT)
        after = ReplayAssessments.for_execution(connection, "exec").require_assessment()
        assert after.facts == ReplayFact.ASSISTANT_OUTPUT | ReplayFact.THINKING_OUTPUT
        assert after.revision == before.revision + 1
        assert not after.allows_retry


def test_retry_start_rollback_restores_prior_replay_authorization(db_path: Path) -> None:
    with ready(db_path, max_attempts=3) as db:
        _, first = started(db)
        first = final_evidence(db, first)
        deferred = db.settle_nonpublication(
            first,
            expected_pointer_revision=1,
            outcome=AttemptFailedAttempt(),
            reason_code=GivenReasonCode("provider_failed"),
            failure=ProviderRetryExhausted("controlled provider failure"),
        )
        assert deferred.value.can_retry
        db.participants.advance_generation(SENDER_LOOKUP, expected_generation=1)
        before = db.get("exec")
        prepared = prepare_fence_token()
        db.session._connection.execute(
            (
                "CREATE TEMP TRIGGER fail_retry_obligation BEFORE UPDATE OF lifecycle ON main"
                ".obligations WHEN NEW.state='pending' BEGIN SELECT RAISE(ABORT, 'injected re"
                "try failure'); END"
            )
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.start(
                AttemptStart(
                    "exec",
                    2,
                    SENDER_LOOKUP,
                    2,
                    prepared,
                    expected_execution_revision=4,
                    expected_pointer_revision=2,
                )
            )
        assert db.get("exec") == before
        assert db.session._connection.execute("SELECT count(*) FROM attempts").fetchone()[0] == 1
        assert not db.session._connection.in_transaction
        db.session._connection.execute("DROP TRIGGER fail_retry_obligation")
        second = db.start(
            AttemptStart(
                "exec",
                2,
                SENDER_LOOKUP,
                2,
                prepared,
                expected_execution_revision=4,
                expected_pointer_revision=2,
            )
        )
        assert isinstance(second, Applied)
        assert second.value.snapshot.replay == before.replay


def test_ambiguity_forces_failed_partition(db_path: Path) -> None:
    with ready(db_path) as db:
        _, fence = started(db)
        with db.session.writing() as connection:
            ReplayAssessments.accumulate(connection, "exec", ReplayFact.TOOL_EXECUTED)
        fence = final_evidence(db, fence)
        settled = db.settle_nonpublication(
            fence, expected_pointer_revision=1, outcome=AttemptFailedAttempt()
        )
        assert type(settled.value.execution.lifecycle) is FailedExecution
        assert not settled.value.can_retry
        assert settled.value.replay.require_assessment().facts == ReplayFact.TOOL_EXECUTED


def test_replay_facts_cannot_be_erased(db_path: Path) -> None:
    with ready(db_path) as db:
        _, fence = started(db)
        db.observe_replay(
            fence,
            expected_pointer_revision=1,
            expected_replay_revision=None,
            facts=ReplayFact.TOOL_EXECUTED,
            replay_safe=False,
            side_effects_possible=True,
        )
        with pytest.raises(IdentityConflict):
            db.observe_replay(
                fence,
                expected_pointer_revision=1,
                expected_replay_revision=1,
                facts=ReplayFact.NONE,
                replay_safe=True,
                side_effects_possible=False,
            )
        assert db.get("exec").replay.require_assessment().facts == ReplayFact.TOOL_EXECUTED


def test_unstarted_failure_and_claim_requeue(db_path: Path) -> None:
    with ready(db_path) as db:
        failed = db.fail_unstarted(
            "exec", expected_revision=2, reason_code=GivenReasonCode("unavailable")
        )
        assert type(failed.value.execution.lifecycle) is FailedExecution
        assert failed.value.attempt is None
        assert type(failed.value.assignments[0].lifecycle) is FailedAssignment
        assert len(failed.value.obligations) == 1
        assert failed.value.obligations[0].lifecycle.declared_name == "failed"
        with pytest.raises(IdentityConflict):
            db.start(
                AttemptStart(
                    "exec",
                    1,
                    SENDER_LOOKUP,
                    1,
                    prepare_fence_token(),
                    expected_execution_revision=3,
                    expected_pointer_revision=0,
                )
            )
    with store(db_path) as reopened:
        assert type(reopened.get("exec").execution.lifecycle) is FailedExecution

    with store(db_path.with_name("claim-only.sqlite3")) as db:
        db.participants.register(SENDER_LOOKUP, committed=True)
        db.assignments.accept(
            WakeAssignment(
                assignment_id="claim",
                recipient="Owner",
                recipient_lookup=SENDER_LOOKUP,
                wire_seq=1,
                message_id=MessageId.mint("message"),
                audience=MessageAudience.DIRECT,
                accepted_at_ms=1_000,
                updated_at_ms=1_000,
                lifecycle=FullPendingAssignment.load(FullWake(), None, None, None),
            )
        )
        deferred = db.assignments.transition_preengagement(
            "claim", DeferredAssignment, expected_revision=1
        )
        assert deferred.value.lifecycle.execution_id is None
        requeued = db.assignments.transition_preengagement(
            "claim", FullPendingAssignment, expected_revision=2
        )
        assert type(requeued.value.lifecycle) is FullPendingAssignment


def test_frozen_v2_pre_attempt_deferred_execution_cannot_resume(db_path: Path) -> None:
    with ready(db_path) as db:
        # Direct SQL establishes a v2-valid parked state that the typed layer
        # deliberately does not expose: ordinal 1 cannot start from DEFERRED.
        with db.session.writing() as connection:
            connection.execute(
                (
                    "UPDATE executions SET lifecycle=json_set(lifecycle,'$.kind','deferred'),revi"
                    "sion=revision+1 WHERE execution_id='exec'"
                )
            )
            connection.execute(
                (
                    "UPDATE obligations SET lifecycle=json_object('kind','deferred'),revision=rev"
                    "ision+1 WHERE execution_id='exec'"
                )
            )
            connection.execute(
                (
                    "UPDATE wake_claims SET lifecycle=json_set(lifecycle,'$.kind','deferred'),rev"
                    "ision=revision+1 WHERE assignment_id='claim'"
                )
            )
        before = db.get("exec")
        assert type(before.execution.lifecycle) is DeferredExecution
        assert before.attempt is None
        with pytest.raises(RecoveryBlocked):
            db.start(
                AttemptStart(
                    "exec",
                    1,
                    SENDER_LOOKUP,
                    1,
                    prepare_fence_token(),
                    expected_execution_revision=3,
                    expected_pointer_revision=0,
                )
            )
        assert db.get("exec") == before
    with store(db_path) as reopened:
        assert type(reopened.get("exec").execution.lifecycle) is DeferredExecution
        assert (
            reopened.session._connection.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
        )


def test_crash_reopen_pending_starts_once_after_owner_returns(db_path: Path) -> None:
    db = ready(db_path)
    before = db.get("exec")
    assert type(before.execution.lifecycle) is PendingExecution
    assert before.attempt is None
    db.close()  # Owner disappeared before the attempt began; PENDING remains durable.
    with store(db_path) as returned:
        assert type(returned.get("exec").execution.lifecycle) is PendingExecution
        returned.participants.advance_generation(SENDER_LOOKUP, expected_generation=1)
        token = prepare_fence_token()
        applied = returned.start(
            AttemptStart(
                "exec",
                1,
                SENDER_LOOKUP,
                2,
                token,
                expected_execution_revision=2,
                expected_pointer_revision=0,
            )
        )
        assert isinstance(applied, Applied)
    # Simulate a lost commit response: the owner has retained the secret but
    # no in-memory result, and must not create a second attempt.
    with store(db_path) as reopened:
        replay = reopened.start(
            AttemptStart(
                "exec",
                1,
                SENDER_LOOKUP,
                2,
                token,
                expected_execution_revision=2,
                expected_pointer_revision=0,
            )
        )
        assert isinstance(replay, AlreadyApplied)
        assert replay.value.snapshot.is_current
        with pytest.raises(StaleRevision):
            reopened.start(
                AttemptStart(
                    "exec",
                    1,
                    SENDER_LOOKUP,
                    2,
                    prepare_fence_token(),
                    expected_execution_revision=2,
                    expected_pointer_revision=0,
                )
            )
        assert (
            reopened.session._connection.execute("SELECT count(*) FROM attempts").fetchone()[0] == 1
        )


def test_frozen_publishing_snapshot_no_nonpublication_settlement(
    db_path: Path
) -> None:
    with ready(db_path) as db:
        _, fence = started(db)
        message = Message(
            sender="thread",
            target="owner",
            body="reply",
            type=InfoKind,
            timestamp=1.0,
        )
        intent = PublicationIntents(
            "exec",
            "owner",
            message,
            canonical_publication_key("exec", "owner"),
            message.message_id,
        )
        with db.session.writing() as connection:
            intent.insert(connection)
            connection.execute(
                (
                    "UPDATE obligations SET lifecycle=json_object('kind','publishing'),revision=r"
                    "evision+1 WHERE execution_id='exec'"
                )
            )
        projected = db.get("exec")
        assert projected.publication_intents == (intent,)
        assert len(projected.obligations) == 1
        assert projected.obligations[0].lifecycle.declared_name == "publishing"
        with pytest.raises(PublicationUncertain):
            db.fail_unknown(fence, expected_pointer_revision=1)
        with pytest.raises(PublicationUncertain):
            db.settle_nonpublication(
                fence,
                expected_pointer_revision=1,
                outcome=AttemptFailedAttempt(),
                reason_code=GivenReasonCode("no_receipt"),
            )
        assert db.get("exec").is_current
        attempt = db.get("exec").attempt
        assert attempt is not None
        assert isinstance(attempt.lifecycle, LiveAttempt)
        assert not attempt.lifecycle.backend_done
        assert db.get("exec").publication_evidence == ()
    with store(db_path) as reopened:
        obligations = reopened.get("exec").obligations
        assert len(obligations) == 1 and obligations[0].lifecycle.declared_name == "publishing"


def test_concurrent_start_exactly_one_claims_pointer(db_path: Path) -> None:
    db = ready(db_path)
    db.close()
    context = multiprocessing.get_context("spawn")
    gate = context.Event()
    output = context.Queue()
    processes = [
        context.Process(
            target=_concurrent_start, args=(str(db_path), prepare_fence_token(), gate, output)
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    gate.set()
    results = [output.get(timeout=10) for _ in processes]
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    assert sorted(results) == ["Applied", "StaleRevision"]
    with store(db_path) as reopened:
        assert reopened.get("exec").is_current
        assert (
            reopened.session._connection.execute("SELECT count(*) FROM attempts").fetchone()[0] == 1
        )


def test_concurrent_same_prepared_token_replays_current_result(db_path: Path) -> None:
    db = ready(db_path)
    db.close()
    context = multiprocessing.get_context("spawn")
    gate = context.Event()
    output = context.Queue()
    token = prepare_fence_token()
    processes = [
        context.Process(target=_concurrent_start, args=(str(db_path), token, gate, output))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    gate.set()
    results = [output.get(timeout=10) for _ in processes]
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    assert sorted(results) == ["AlreadyApplied", "Applied"]
    with store(db_path) as reopened:
        assert reopened.get("exec").attempt is not None
        assert (
            reopened.session._connection.execute("SELECT count(*) FROM attempts").fetchone()[0] == 1
        )


def test_concurrent_registration_one_writer_one_replay(db_path: Path) -> None:
    # POSIX spawn avoids inheriting any open SQLite connection or lock.
    context = multiprocessing.get_context("spawn")
    gate = context.Event()
    output = context.Queue()
    processes = [
        context.Process(target=_concurrent_registration, args=(str(db_path), gate, output))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    gate.set()
    results = [output.get(timeout=10) for _ in processes]
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    assert sorted(results) == ["AlreadyApplied", "Applied"]
    with store(db_path) as db:
        assert db.participants.get(SENDER_LOOKUP).owner_identity.generation == 1
        assert (
            db.session._connection.execute("SELECT count(*) FROM participants").fetchone()[0] == 1
        )


def _read_while_other_store_waits[Result, Written](
    path: Path,
    first_table: str,
    read: Callable[[StandaloneCoordination], Result],
    write: Callable[[StandaloneCoordination], Written],
) -> Result:
    """Hold a reader while a second connection waits for write claim_scope."""
    first_read = threading.Event()
    writer_ready = threading.Event()
    write_started = threading.Event()
    writer_done = threading.Event()
    errors: list[BaseException] = []

    class PausedReader(StandaloneCoordination):
        def __init__(self, path: str, *, clock_ms: Callable[[], int]) -> None:
            super().__init__(path, clock_ms=clock_ms)
            paused = False
            selected = False

            def trace(statement: str) -> None:
                nonlocal selected
                selected = f"FROM {first_table} " in statement or (
                    f'FROM "{first_table}" ' in statement
                )

            def read_row(cursor: sqlite3.Cursor, values: tuple[str | int | float | bytes | None, ...]) -> sqlite3.Row:
                nonlocal paused
                result = sqlite3.Row(cursor, values)
                if selected and not paused:
                    paused = True
                    first_read.set()
                    assert write_started.wait(timeout=5), "writer never requested claim_scope"
                    # A real SQLite row was read under the aggregate snapshot.
                    # The second connection must remain blocked before changing state
                    # until its reader exits.
                    writer_done.wait(timeout=0.3)
                return result

            self.session._connection.set_trace_callback(trace)
            self.session._connection.row_factory = read_row

    def writer() -> None:
        try:
            with store(path) as other:

                def trace(statement: str) -> None:
                    if statement == "BEGIN EXCLUSIVE":
                        write_started.set()

                other.session._connection.set_trace_callback(trace)
                writer_ready.set()
                assert first_read.wait(timeout=5), "reader never reached its first SELECT"
                write(other)
        except BaseException as exc:
            errors.append(exc)
        finally:
            writer_done.set()

    worker = threading.Thread(target=writer)
    with PausedReader(str(path), clock_ms=lambda: 1_000) as reader:
        worker.start()
        try:
            assert writer_ready.wait(timeout=5), "writer never opened its connection"
            result = read(reader)
        finally:
            worker.join(timeout=5)
    assert not worker.is_alive(), "writer remained blocked after reader completed"
    assert not errors
    return result


def test_public_snapshot_is_one_committed_revision_during_settlement(db_path: Path) -> None:
    with ready(db_path) as db:
        _, fence = started(db)
        fence = final_evidence(db, fence)

    observed = _read_while_other_store_waits(
        db_path,
        "executions",
        lambda reader: reader.get("exec"),
        lambda writer: writer.settle_nonpublication(
            fence,
            expected_pointer_revision=1,
            outcome=AttemptFailedAttempt(),
            reason_code=GivenReasonCode("failure"),
        ),
    )
    assert type(observed.execution.lifecycle) is ActiveExecution
    assert observed.attempt is not None
    assert type(observed.attempt.lifecycle) is SettlingAttempt
    assert observed.is_current
    with store(db_path) as db:
        assert type(db.get("exec").execution.lifecycle) is FailedExecution


def test_public_participant_is_one_committed_revision_during_commit(db_path: Path) -> None:
    with store(db_path) as db:
        db.participants.register(SENDER_LOOKUP, committed=False)

    observed = _read_while_other_store_waits(
        db_path,
        "participants",
        lambda reader: reader.participants.get(SENDER_LOOKUP),
        lambda writer: writer.participants.commit(SENDER_LOOKUP),
    )
    assert not observed.committed
    with store(db_path) as db:
        assert db.participants.get(SENDER_LOOKUP).committed


def test_public_read_exception_releases_transaction_and_mutators_reuse_it(db_path: Path) -> None:
    with ready(db_path) as db:
        with pytest.raises(IdentityConflict):
            db.get("missing")
        assert not db.session._connection.in_transaction
        with pytest.raises(IdentityConflict):
            db.participants.get(UNRELATED_LOOKUP)
        assert not db.session._connection.in_transaction
        with db.session.writing():
            assert db.session._connection.in_transaction
            assert type(db.get("exec").execution.lifecycle) is PendingExecution
            assert db.participants.get(SENDER_LOOKUP).committed
            assert db.session._connection.in_transaction
        assert not db.session._connection.in_transaction
        db.participants.register(RECIPIENT_LOOKUP)
        assert db.participants.get(RECIPIENT_LOOKUP).lookup == RECIPIENT_LOOKUP


def test_observation_closes_failed_reads_and_preserves_schema_disposition(db_path: Path) -> None:
    from agent_comms.coordination_database import CoordinationStore

    with store(db_path) as writer:
        writer.participants.register(SENDER_LOOKUP, committed=True)
    for statement in (
        "UPDATE participants SET committed=0",
        "SELECT * FROM nonexistent_original_table",
    ):
        with pytest.raises(sqlite3.OperationalError):
            with CoordinationStore.observing(db_path) as reader:
                reader.execute(statement)
        with pytest.raises(sqlite3.ProgrammingError):
            reader.execute("SELECT 1")
    with pytest.raises(RuntimeError):
        with CoordinationStore.observing(db_path) as reader:
            raise RuntimeError("consumer failed")
    with pytest.raises(sqlite3.ProgrammingError):
        reader.execute("SELECT 1")
    with store(db_path) as writer:
        assert writer.participants.get(SENDER_LOOKUP).committed


