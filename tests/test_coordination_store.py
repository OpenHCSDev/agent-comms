"""Slice-2 nonpublication mutation gates against the frozen schema."""

from __future__ import annotations

import hashlib
import multiprocessing
import sqlite3
import threading
from agent_comms.reason_codes import GivenDetail, GivenReasonCode, NoDetail
from agent_comms.attempt_states import AttemptFailedAttempt, SucceededAttempt
from collections.abc import Callable
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
from agent_comms.attempt_start import AttemptStart
from agent_comms.attempt_states import (
    AbortingAttempt,
    ModelRunningAttempt,
    ModelStalledAttempt,
    PromptAcceptedAttempt,
    PromptStartingAttempt,
    RetryingAttempt,
    SettlingAttempt,
)
from agent_comms.attempt_start import INITIAL_LEASE_DURATION_MS
from agent_comms.coordination_errors import (
    IdentityConflict,
    IntegrityViolationError,
    PublicationUncertain,
    RecoveryBlocked,
    StaleFence,
    StaleRevision,
)
from agent_comms.coordination_results import AlreadyApplied, Applied
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.coordination_tables.attempts import ReplayFact
from agent_comms.coordination_tables.executions import (
    NoReplayAssessment,
    ReplayAssessment,
    ReplayAssessments,
)
from agent_comms.coordination_tables.executions import ExecutionOrigin
from agent_comms.coordination_tables.publications import (
    PublicationIntents,
)
from agent_comms.coordination_tables.recovery import ACPClientConnectivity, OwnerConnectivity
from agent_comms.coordinator import StandaloneCoordination
from agent_comms.response_acknowledgement import canonical_publication_key
from agent_comms.execution_states import (
    ActiveExecution,
    CompletedExecution,
    DeferredExecution,
    FailedExecution,
    PendingExecution,
)
from agent_comms.field_codec import FieldCodec
from agent_comms.messages import Message, InfoKind
from agent_comms.owner_fence import prepare_fence_token
from agent_comms.recovery_states import FailedRecovery, ModelStalledRecovery, RecoveredRecovery
from agent_comms.turn_failure import ProviderRetryExhausted
from agent_comms.wake_policy import BoundedTriageWake, FullWake, MessageAudience, PassiveWake, WakePolicy


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "coordination.sqlite3"


def store(path: Path) -> StandaloneCoordination:
    return StandaloneCoordination(str(path), clock_ms=lambda: 1_000)


def wire_source(db, assignment_id: str, original: Message, mode: WakePolicy):
    from agent_comms.audience_manifest import FrozenRecipient, freeze_audience
    from agent_comms.bus_publication import CommittedDelivery, decisions_digest, decisions_wire
    from agent_comms.coordination_contracts import POLICY_VERSION, RESOLVER_VERSION
    from agent_comms.selected_source_batch import SelectedSource
    from agent_comms.wake_policy import WakeDecision

    audience = freeze_audience(original, (FrozenRecipient("owner", "Owner"),),
                               "0" * 64, sender_lookup="sender",
                               sender_name=original.sender)
    decisions = (WakeDecision("owner", MessageAudience.DIRECT, mode),)
    delivery = CommittedDelivery("0" * 32, original, audience, decisions,
                                 decisions_digest(decisions_wire(decisions)), "ordinary",
                                 RESOLVER_VERSION, POLICY_VERSION)
    return SelectedSource(assignment_id, db.assignments, delivery)


def ready(
    path: Path, *, origin: ExecutionOrigin = ExecutionOrigin.WIRE, max_attempts: int = 2
) -> StandaloneCoordination:
    db = store(path)
    sources = ()
    assert isinstance(db.participants.register("owner", "Owner", "thread", committed=True), Applied)
    if origin is ExecutionOrigin.WIRE:
        original = Message("owner", "Owner", "Original request", InfoKind, timestamp=1.0, seq=1)
        db.assignments.accept(
            WakeAssignment(
                assignment_id="claim",
                recipient="Owner",
                recipient_lookup="owner",
                wire_seq=1,
                message_id=original.message_id,
                audience=MessageAudience.DIRECT,
                accepted_at_ms=1_000,
                updated_at_ms=1_000,
                lifecycle=FullPendingAssignment.load(FullWake(), None, None, None),
            )
        )
        sources = (wire_source(db, "claim", original, FullWake()),)
    db.executions.create(
        "exec",
        origin,
        "owner",
        "thread",
        max_attempts,
        sources=sources,
    )
    db.executions.mark_pending("exec", expected_revision=1)
    return db


def started(db: StandaloneCoordination) -> tuple[str, object]:
    token = prepare_fence_token()
    result = db.attempts.start(
        AttemptStart(
            "exec",
            1,
            "thread",
            1,
            token,
            expected_execution_revision=2,
            expected_pointer_revision=0,
        )
    )
    assert isinstance(result, Applied)
    return token, result.value.fence


def final_evidence(db: StandaloneCoordination, fence: object, pointer_revision: int = 1) -> object:
    from agent_comms.owner_fence import OwnerFence

    assert isinstance(fence, OwnerFence)
    result = db.attempts.advance(
        fence, PromptAcceptedAttempt, expected_pointer_revision=pointer_revision
    )
    result = db.attempts.advance(
        result.value.fence, ModelRunningAttempt, expected_pointer_revision=pointer_revision
    )
    result = db.attempts.advance(
        result.value.fence,
        SettlingAttempt,
        expected_pointer_revision=pointer_revision,
        backend_done=True,
        process_dead=True,
    )
    return result.value.fence


def _concurrent_registration(
    path: str, gate: multiprocessing.Event, output: multiprocessing.Queue
) -> None:
    try:
        with StandaloneCoordination(path, clock_ms=lambda: 1_000) as db:
            gate.wait(timeout=5)
            result = db.participants.register("owner", "Owner", "thread", committed=True)
            output.put(type(result).__name__)
    except Exception as exc:
        output.put(type(exc).__name__)


def _concurrent_start(
    path: str, token: str, gate: multiprocessing.Event, output: multiprocessing.Queue
) -> None:
    try:
        with StandaloneCoordination(path, clock_ms=lambda: 1_000) as db:
            gate.wait(timeout=5)
            result = db.attempts.start(
                AttemptStart(
                    "exec",
                    1,
                    "thread",
                    1,
                    token,
                    expected_execution_revision=2,
                    expected_pointer_revision=0,
                )
            )
            output.put(type(result).__name__)
    except Exception as exc:
        output.put(type(exc).__name__)


def test_registration_complete_atomic_reopen_and_rename(db_path: Path) -> None:
    db = store(db_path)
    original = db.participants.register("owner", "Owner", "thread", committed=False)
    assert isinstance(original, Applied)
    assert original.value.pointer.pointer_revision == 0
    assert original.value.aliases == ("owner",)
    assert isinstance(db.participants.register("owner", "Owner", "thread"), AlreadyApplied)
    assert isinstance(db.participants.commit("owner"), Applied)
    assert isinstance(db.participants.commit("owner"), AlreadyApplied)
    rename = db.participants.rename("owner", "Owner New", "Owner New", expected_generation=1)
    assert rename.value.aliases == ("owner", "Owner New")
    assert isinstance(
        db.participants.rename("owner", "Owner New", "Owner New", expected_generation=1),
        AlreadyApplied,
    )
    assert isinstance(
        db.participants.advance_generation("owner", "thread", expected_generation=1), Applied
    )
    with pytest.raises(StaleRevision):
        db.participants.advance_generation("owner", "thread", expected_generation=1)
    db.close()
    with store(db_path) as reopened:
        assert reopened.participants.get("owner").owner_identity.generation == 2
        assert reopened.participants.get("owner").committed
        assert reopened.participants.get("owner").aliases == ("owner", "Owner New")
        replay = reopened.participants.register("owner", "Owner", "thread", committed=False)
        assert isinstance(replay, AlreadyApplied)
        assert replay.value.owner_identity.generation == 2
        assert replay.value.display_name == "Owner New"


def test_registration_transaction_rollback_on_alias_collision(db_path: Path) -> None:
    with store(db_path) as db:
        db.participants.register("first", "First", "thread", alias="alias")
        with pytest.raises(IdentityConflict):
            db.participants.register("second", "Second", "thread", alias="alias")
        assert (
            db.session._connection.execute("SELECT count(*) FROM participants").fetchone()[0] == 1
        )
        assert not db.session._connection.in_transaction
    with store(db_path) as db:
        assert (
            db.session._connection.execute("SELECT count(*) FROM owner_generations").fetchone()[0]
            == 1
        )
        assert (
            db.session._connection.execute("SELECT count(*) FROM current_executions").fetchone()[0]
            == 1
        )


def test_claim_execution_idempotency_and_rollback(db_path: Path) -> None:
    db = ready(db_path)
    snap = db.snapshots.get("exec")
    assert type(snap.execution.lifecycle) is PendingExecution
    assert type(snap.assignments[0].lifecycle) is EngagedAssignment
    assert snap.links[0].ordinal == 0
    assert len(snap.obligations) == 1
    original = Message("owner", "Owner", "Original request", InfoKind, timestamp=1.0, seq=1)
    replay = db.executions.create(
        "exec",
        ExecutionOrigin.WIRE,
        "owner",
        "thread",
        2,
        sources=(wire_source(db, "claim", original, FullWake()),),
    )
    assert isinstance(replay, AlreadyApplied)
    different = Message("different", "Owner", "Original request", InfoKind,
                        timestamp=1.0, seq=1)
    with pytest.raises(IdentityConflict):
        db.executions.create(
            "exec",
            ExecutionOrigin.WIRE,
            "owner",
            "thread",
            2,
            sources=(wire_source(db, "claim", different, FullWake()),),
        )
    db.close()
    with store(db_path) as reopened:
        assert reopened.snapshots.get("exec").links == snap.links


def test_claim_acceptance_requires_initial_mode_decision(db_path: Path) -> None:
    with store(db_path) as db:
        db.participants.register("owner", "Owner", "thread", committed=True)

        def assignment(
            index: int,
            mode: WakePolicy,
            disposition: AssignmentState,
            verdict: str | None = None,
        ) -> WakeAssignment:
            return WakeAssignment(
                assignment_id=f"claim-{index}",
                recipient="Owner",
                recipient_lookup="owner",
                wire_seq=index,
                message_id=f"message-{index}",
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
        for index, mode, disposition in (
            (6, PassiveWake(), PassiveAssignment),
            (7, BoundedTriageWake(), TriagePendingAssignment),
            (8, FullWake(), FullPendingAssignment),
            (9, FullWake(), FullPendingAssignment),
        ):
            initial = assignment(index, mode, disposition)
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
        db.participants.register("owner", "Owner", "thread", committed=True)
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
                    recipient_lookup="owner",
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
            db.executions.create(
                "exec",
                ExecutionOrigin.WIRE,
                "owner",
                "thread",
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
        first = db.attempts.start(
            AttemptStart(
                "exec",
                1,
                "thread",
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
        assert (
            attempt.lifecycle.lease_expires_at_ms
            == attempt.created_at_ms + INITIAL_LEASE_DURATION_MS
        )
        assert token not in str(FieldCodec.project(first.value.snapshot, "snapshot"))
        assert token not in db_path.read_bytes().decode("latin1")
        duplicate = db.attempts.start(
            AttemptStart(
                "exec",
                1,
                "thread",
                1,
                token,
                expected_execution_revision=2,
                expected_pointer_revision=0,
            )
        )
        assert isinstance(duplicate, AlreadyApplied)
        progressed = db.attempts.advance(
            first.value.fence, PromptAcceptedAttempt, expected_pointer_revision=1
        )
        renewed = db.attempts.renew_lease(
            progressed.value.fence, expected_pointer_revision=1, duration_ms=120_000
        )
        assert renewed.value.snapshot.attempt is not None
        assert renewed.value.snapshot.attempt.lifecycle.lease_expires_at_ms >= 121_000
        duplicate = db.attempts.start(
            AttemptStart(
                "exec",
                1,
                "thread",
                1,
                token,
                expected_execution_revision=2,
                expected_pointer_revision=0,
            )
        )
        assert isinstance(duplicate, AlreadyApplied)
        assert duplicate.value.snapshot.attempt == renewed.value.snapshot.attempt
        with pytest.raises(IdentityConflict):
            db.attempts.start(
                AttemptStart(
                    "exec",
                    2,
                    "thread",
                    1,
                    token,
                    expected_execution_revision=2,
                    expected_pointer_revision=0,
                )
            )
        with pytest.raises(StaleRevision):
            db.attempts.advance(first.value.fence, ModelRunningAttempt, expected_pointer_revision=1)
    with store(db_path) as reopened:
        duplicate = reopened.attempts.start(
            AttemptStart(
                "exec",
                1,
                "thread",
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
        db.participants.register("other", "Other", "other-thread", committed=True)
        db.executions.create("other-exec", ExecutionOrigin.GOAL, "other", "other-thread", 1)
        db.executions.mark_pending("other-exec", expected_revision=1)
        with pytest.raises(IdentityConflict):
            db.attempts.start(
                AttemptStart(
                    "other-exec",
                    1,
                    "other-thread",
                    1,
                    token,
                    expected_execution_revision=2,
                    expected_pointer_revision=0,
                )
            )
        assert db.snapshots.get("other-exec").attempt is None
        assert (
            db.session._connection.execute("SELECT owner_token_digest FROM attempts").fetchone()[0]
            == hashlib.sha256(token.encode()).hexdigest()
        )


def test_settled_snapshot_encodes_recovery_kind_separately_from_table(db_path: Path) -> None:
    from agent_comms.coordination_snapshot import RecoverySnapshot
    from agent_comms.coordination_tables.recovery import RecoveryAudit

    with store(db_path) as db:
        db.participants.register("owner", "Owner", "thread", committed=True)
        db.executions.create("exec", ExecutionOrigin.ACP, "owner", "thread", 1)
        db.executions.mark_pending("exec", expected_revision=1)
        _, fence = started(db)
        fence = final_evidence(db, fence)
        settled = db.attempts.settle_nonpublication(
            fence, expected_pointer_revision=1, outcome=AttemptFailedAttempt(),
            reason_code=GivenReasonCode("failed"))
        # Populate the original audit table for serialization, not owner-loss proof.
        with db.session.writing() as connection:
            RecoveryAudit(execution_id="exec", kind=FailedRecovery, reason_code=GivenReasonCode("failed"),
                sanitized_detail=NoDetail(), attempt=1, elapsed_ms=0, observed_at_ms=1_000).insert(connection)
        observed = db.snapshots.get("exec")
        assert observed.execution == settled.value.execution
        encoded = FieldCodec.encode(observed)
        assert encoded["last_recovery"]["row"] == RecoveryAudit.declared_name
        assert encoded["last_recovery"]["kind"] == FailedRecovery.declared_name
        assert FieldCodec.decode(RecoverySnapshot, encoded) == observed
        assert FieldCodec.decode(RecoveryAudit, encoded["last_recovery"]) == observed.last_recovery
        assert not observed.is_current and not observed.can_retry


def test_nonpublication_claimless_atomic_settlement(db_path: Path) -> None:
    with ready(db_path, origin=ExecutionOrigin.ACP) as db:
        _, fence = started(db)
        fence = final_evidence(db, fence)
        settled = db.attempts.settle_nonpublication(
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
        assert type(reopened.snapshots.get("exec").execution.lifecycle) is CompletedExecution


def test_retry_partition_and_generation_fence(db_path: Path) -> None:
    with ready(db_path) as db:
        token, fence = started(db)
        db.attempts.observe_replay(
            fence,
            expected_pointer_revision=1,
            expected_replay_revision=None,
            facts=ReplayFact.NONE,
            replay_safe=True,
            side_effects_possible=False,
        )
        fence = final_evidence(db, fence)
        outcome = db.attempts.settle_nonpublication(
            fence,
            expected_pointer_revision=1,
            outcome=AttemptFailedAttempt(),
            reason_code=GivenReasonCode("provider_failed"),
        )
        assert type(outcome.value.execution.lifecycle) is DeferredExecution
        assert outcome.value.can_retry
        with pytest.raises(StaleFence):
            db.attempts.start(
                AttemptStart(
                    "exec",
                    2,
                    "thread",
                    1,
                    prepare_fence_token(),
                    expected_execution_revision=4,
                    expected_pointer_revision=2,
                )
            )
        db.participants.advance_generation("owner", "thread", expected_generation=1)
        second = db.attempts.start(
            AttemptStart(
                "exec",
                2,
                "thread",
                2,
                prepare_fence_token(),
                expected_execution_revision=4,
                expected_pointer_revision=2,
            )
        )
        assert second.value.snapshot.attempt is not None
        assert second.value.snapshot.attempt.attempt_ordinal == 2
        with pytest.raises(StaleFence):
            db.attempts.advance(fence, ModelRunningAttempt, expected_pointer_revision=3)
        assert isinstance(
            db.attempts.start(
                AttemptStart(
                    "exec",
                    1,
                    "thread",
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
        outcome = db.attempts.settle_nonpublication(
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
        first = db.attempts.advance(
            fence,
            PromptStartingAttempt,
            expected_pointer_revision=1,
            **{first_fact: True},
        )
        fence = first.value.fence
        before = db.snapshots.get("exec")
        for phase, progress in (
            (PromptAcceptedAttempt, False),
            (PromptStartingAttempt, True),
            (PromptStartingAttempt, False),
        ):
            with pytest.raises(RecoveryBlocked):
                db.attempts.advance(fence, phase, expected_pointer_revision=1, progress=progress)
            assert db.snapshots.get("exec") == before
        if first_fact == "process_dead":
            with pytest.raises(RecoveryBlocked):
                db.attempts.renew_lease(fence, expected_pointer_revision=1, duration_ms=1_000)
            assert db.snapshots.get("exec") == before
            second_fact = "backend_done"
        else:
            # A done-but-live child may still be waiting for verified exit.
            renewed = db.attempts.renew_lease(fence, expected_pointer_revision=1, duration_ms=1_000)
            fence = renewed.value.fence
            second_fact = "process_dead"
        final = db.attempts.advance(
            fence,
            PromptStartingAttempt,
            expected_pointer_revision=1,
            **{second_fact: True},
        )
        fence = final.value.fence
        assert final.value.snapshot.attempt is not None
        assert final.value.snapshot.attempt.lifecycle.backend_done
        assert final.value.snapshot.attempt.lifecycle.process_dead
        with pytest.raises(RecoveryBlocked):
            db.attempts.advance(fence, PromptStartingAttempt, expected_pointer_revision=1)
        outcome = db.attempts.settle_nonpublication(
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
        deferred = db.attempts.settle_nonpublication(
            first,
            expected_pointer_revision=1,
            outcome=AttemptFailedAttempt(),
            reason_code=GivenReasonCode("provider_failed"),
            failure=ProviderRetryExhausted("controlled provider failure"),
        )
        proof = deferred.value.replay.require_assessment()
        assert deferred.value.can_retry and proof.allows_retry
        db.participants.advance_generation("owner", "thread", expected_generation=1)
        second = db.attempts.start(
            AttemptStart(
                "exec",
                2,
                "thread",
                2,
                prepare_fence_token(),
                expected_execution_revision=4,
                expected_pointer_revision=2,
            )
        )
        assert second.value.snapshot.replay == proof
        with db.session.writing() as connection:
            ReplayAssessments.accumulate(connection, "exec", fact)
        revoked = db.snapshots.get("exec").replay.require_assessment()
        assert revoked.facts == fact and not revoked.allows_retry

        second_fence = final_evidence(db, second.value.fence, pointer_revision=3)
        outcome = db.attempts.settle_nonpublication(
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
        absent = store.snapshots.get("exec").replay
        assert absent == NoReplayAssessment()
        assert FieldCodec.encode(absent, ReplayAssessment) is None
        assert FieldCodec.decode(ReplayAssessment, None) == absent
        snapshot = store.snapshots.get("exec")
        encoded = FieldCodec.encode(snapshot)
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
        deferred = db.attempts.settle_nonpublication(
            first,
            expected_pointer_revision=1,
            outcome=AttemptFailedAttempt(),
            reason_code=GivenReasonCode("provider_failed"),
            failure=ProviderRetryExhausted("controlled provider failure"),
        )
        assert deferred.value.can_retry
        db.participants.advance_generation("owner", "thread", expected_generation=1)
        before = db.snapshots.get("exec")
        prepared = prepare_fence_token()
        db.session._connection.execute(
            (
                "CREATE TEMP TRIGGER fail_retry_obligation BEFORE UPDATE OF lifecycle ON main"
                ".obligations WHEN NEW.state='pending' BEGIN SELECT RAISE(ABORT, 'injected re"
                "try failure'); END"
            )
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.attempts.start(
                AttemptStart(
                    "exec",
                    2,
                    "thread",
                    2,
                    prepared,
                    expected_execution_revision=4,
                    expected_pointer_revision=2,
                )
            )
        assert db.snapshots.get("exec") == before
        assert db.session._connection.execute("SELECT count(*) FROM attempts").fetchone()[0] == 1
        assert not db.session._connection.in_transaction
        db.session._connection.execute("DROP TRIGGER fail_retry_obligation")
        second = db.attempts.start(
            AttemptStart(
                "exec",
                2,
                "thread",
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
        db.attempts.observe_replay(
            fence,
            expected_pointer_revision=1,
            expected_replay_revision=None,
            facts=ReplayFact.TOOL_EXECUTED,
            replay_safe=False,
            side_effects_possible=True,
        )
        fence = final_evidence(db, fence)
        settled = db.attempts.settle_nonpublication(
            fence, expected_pointer_revision=1, outcome=AttemptFailedAttempt()
        )
        assert type(settled.value.execution.lifecycle) is FailedExecution
        assert not settled.value.can_retry
        assert settled.value.replay.require_assessment().facts == ReplayFact.TOOL_EXECUTED


def test_fenced_connectivity_audit_and_monotone_replay(db_path: Path) -> None:
    with ready(db_path) as db:
        _, fence = started(db)
        observed = db.attempts.observe_connectivity(
            fence,
            expected_pointer_revision=1,
            expected_revision=None,
            owner=OwnerConnectivity.RECONNECTING,
            acp_client=ACPClientConnectivity.DISCONNECTED,
        )
        assert observed.value.connectivity is not None
        assert observed.value.connectivity.revision == 1
        with pytest.raises(StaleRevision):
            db.attempts.observe_connectivity(
                fence,
                expected_pointer_revision=1,
                expected_revision=None,
                owner=OwnerConnectivity.OFFLINE,
                acp_client=ACPClientConnectivity.DISCONNECTED,
            )
        with pytest.raises(IdentityConflict):
            db.attempts.append_recovery_audit(
                fence, expected_pointer_revision=1, kind=FailedRecovery, reason_code=GivenReasonCode("invented")
            )
        accepted = db.attempts.advance(fence, PromptAcceptedAttempt, expected_pointer_revision=1)
        stalled = db.attempts.advance(
            accepted.value.fence, ModelStalledAttempt, expected_pointer_revision=1
        )
        fence = stalled.value.fence
        audited = db.attempts.append_recovery_audit(
            fence,
            expected_pointer_revision=1,
            kind=ModelStalledRecovery,
            reason_code=GivenReasonCode("stalled"),
            sanitized_detail=GivenDetail("watchdog"),
            elapsed_ms=1_000,
        )
        assert audited.value.last_recovery is not None
        assert audited.value.last_recovery.attempt == 1
        db.attempts.observe_replay(
            fence,
            expected_pointer_revision=1,
            expected_replay_revision=None,
            facts=ReplayFact.TOOL_EXECUTED,
            replay_safe=False,
            side_effects_possible=True,
        )
        with pytest.raises(IdentityConflict):
            db.attempts.observe_replay(
                fence,
                expected_pointer_revision=1,
                expected_replay_revision=1,
                facts=ReplayFact.NONE,
                replay_safe=True,
                side_effects_possible=False,
            )
        assert db.snapshots.get("exec").replay.require_assessment().facts == ReplayFact.TOOL_EXECUTED


def test_recovered_audit_requires_same_attempt_new_incident_and_resumption(db_path: Path) -> None:
    with ready(db_path) as db:
        _, fence = started(db)
        accepted = db.attempts.advance(fence, PromptAcceptedAttempt, expected_pointer_revision=1)
        stalled = db.attempts.advance(
            accepted.value.fence, ModelStalledAttempt, expected_pointer_revision=1
        )
        fence = stalled.value.fence
        db.attempts.append_recovery_audit(
            fence,
            expected_pointer_revision=1,
            kind=ModelStalledRecovery,
            reason_code=GivenReasonCode("stalled"),
        )
        with pytest.raises(IdentityConflict):
            db.attempts.append_recovery_audit(
                fence,
                expected_pointer_revision=1,
                kind=RecoveredRecovery,
                reason_code=GivenReasonCode("premature"),
            )
        aborting = db.attempts.advance(fence, AbortingAttempt, expected_pointer_revision=1)
        with pytest.raises(IdentityConflict):
            db.attempts.append_recovery_audit(
                aborting.value.fence,
                expected_pointer_revision=1,
                kind=RecoveredRecovery,
                reason_code=GivenReasonCode("still_aborting"),
            )
        retrying = db.attempts.advance(
            aborting.value.fence, RetryingAttempt, expected_pointer_revision=1
        )
        running = db.attempts.advance(
            retrying.value.fence, ModelRunningAttempt, expected_pointer_revision=1
        )
        fence = running.value.fence
        db.attempts.append_recovery_audit(
            fence,
            expected_pointer_revision=1,
            kind=RecoveredRecovery,
            reason_code=GivenReasonCode("resumed"),
        )
        with pytest.raises(IdentityConflict):
            db.attempts.append_recovery_audit(
                fence,
                expected_pointer_revision=1,
                kind=RecoveredRecovery,
                reason_code=GivenReasonCode("duplicate"),
            )
        again = db.attempts.advance(fence, ModelStalledAttempt, expected_pointer_revision=1)
        db.attempts.append_recovery_audit(
            again.value.fence,
            expected_pointer_revision=1,
            kind=ModelStalledRecovery,
            reason_code=GivenReasonCode("stalled_again"),
        )
        aborting = db.attempts.advance(
            again.value.fence, AbortingAttempt, expected_pointer_revision=1
        )
        retrying = db.attempts.advance(
            aborting.value.fence, RetryingAttempt, expected_pointer_revision=1
        )
        running = db.attempts.advance(
            retrying.value.fence, ModelRunningAttempt, expected_pointer_revision=1
        )
        db.attempts.append_recovery_audit(
            running.value.fence,
            expected_pointer_revision=1,
            kind=RecoveredRecovery,
            reason_code=GivenReasonCode("resumed_again"),
        )


def test_recovered_audit_rejects_prior_attempt_incident_and_terminal(db_path: Path) -> None:
    with ready(db_path) as db:
        _, first = started(db)
        accepted = db.attempts.advance(first, PromptAcceptedAttempt, expected_pointer_revision=1)
        stalled = db.attempts.advance(
            accepted.value.fence, ModelStalledAttempt, expected_pointer_revision=1
        )
        db.attempts.append_recovery_audit(
            stalled.value.fence,
            expected_pointer_revision=1,
            kind=ModelStalledRecovery,
            reason_code=GivenReasonCode("stalled"),
        )
        db.attempts.observe_replay(
            stalled.value.fence,
            expected_pointer_revision=1,
            expected_replay_revision=None,
            facts=ReplayFact.NONE,
            replay_safe=True,
            side_effects_possible=False,
        )
        done = db.attempts.advance(
            stalled.value.fence,
            ModelStalledAttempt,
            expected_pointer_revision=1,
            backend_done=True,
            process_dead=True,
        )
        deferred = db.attempts.settle_nonpublication(
            done.value.fence,
            expected_pointer_revision=1,
            outcome=AttemptFailedAttempt(),
            reason_code=GivenReasonCode("first_failed"),
        )
        assert deferred.value.can_retry
        with pytest.raises(StaleFence):
            db.attempts.append_recovery_audit(
                done.value.fence,
                expected_pointer_revision=2,
                kind=RecoveredRecovery,
                reason_code=GivenReasonCode("terminal"),
            )
        db.participants.advance_generation("owner", "thread", expected_generation=1)
        second = db.attempts.start(
            AttemptStart(
                "exec",
                2,
                "thread",
                2,
                prepare_fence_token(),
                expected_execution_revision=4,
                expected_pointer_revision=2,
            )
        )
        accepted = db.attempts.advance(
            second.value.fence, PromptAcceptedAttempt, expected_pointer_revision=3
        )
        running = db.attempts.advance(
            accepted.value.fence, ModelRunningAttempt, expected_pointer_revision=3
        )
        with pytest.raises(IdentityConflict):
            db.attempts.append_recovery_audit(
                running.value.fence,
                expected_pointer_revision=3,
                kind=RecoveredRecovery,
                reason_code=GivenReasonCode("prior_attempt"),
            )


def test_unstarted_failure_and_claim_requeue(db_path: Path) -> None:
    with ready(db_path) as db:
        failed = db.executions.fail_unstarted(
            "exec", expected_revision=2, reason_code=GivenReasonCode("unavailable")
        )
        assert type(failed.value.execution.lifecycle) is FailedExecution
        assert failed.value.attempt is None
        assert type(failed.value.assignments[0].lifecycle) is FailedAssignment
        assert len(failed.value.obligations) == 1
        assert failed.value.obligations[0].lifecycle.declared_name == "failed"
        with pytest.raises(IdentityConflict):
            db.attempts.start(
                AttemptStart(
                    "exec",
                    1,
                    "thread",
                    1,
                    prepare_fence_token(),
                    expected_execution_revision=3,
                    expected_pointer_revision=0,
                )
            )
    with store(db_path) as reopened:
        assert type(reopened.snapshots.get("exec").execution.lifecycle) is FailedExecution

    with store(db_path.with_name("claim-only.sqlite3")) as db:
        db.participants.register("owner", "Owner", "thread", committed=True)
        db.assignments.accept(
            WakeAssignment(
                assignment_id="claim",
                recipient="Owner",
                recipient_lookup="owner",
                wire_seq=1,
                message_id="message",
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
        before = db.snapshots.get("exec")
        assert type(before.execution.lifecycle) is DeferredExecution
        assert before.attempt is None
        with pytest.raises(RecoveryBlocked):
            db.attempts.start(
                AttemptStart(
                    "exec",
                    1,
                    "thread",
                    1,
                    prepare_fence_token(),
                    expected_execution_revision=3,
                    expected_pointer_revision=0,
                )
            )
        assert db.snapshots.get("exec") == before
    with store(db_path) as reopened:
        assert type(reopened.snapshots.get("exec").execution.lifecycle) is DeferredExecution
        assert (
            reopened.session._connection.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
        )


def test_crash_reopen_pending_starts_once_after_owner_returns(db_path: Path) -> None:
    db = ready(db_path)
    before = db.snapshots.get("exec")
    assert type(before.execution.lifecycle) is PendingExecution
    assert before.attempt is None
    db.close()  # Owner disappeared before the attempt began; PENDING remains durable.
    with store(db_path) as returned:
        assert type(returned.snapshots.get("exec").execution.lifecycle) is PendingExecution
        returned.participants.advance_generation("owner", "thread", expected_generation=1)
        token = prepare_fence_token()
        applied = returned.attempts.start(
            AttemptStart(
                "exec",
                1,
                "thread",
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
        replay = reopened.attempts.start(
            AttemptStart(
                "exec",
                1,
                "thread",
                2,
                token,
                expected_execution_revision=2,
                expected_pointer_revision=0,
            )
        )
        assert isinstance(replay, AlreadyApplied)
        assert replay.value.snapshot.is_current
        with pytest.raises(StaleRevision):
            reopened.attempts.start(
                AttemptStart(
                    "exec",
                    1,
                    "thread",
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
        projected = db.snapshots.get("exec")
        assert projected.publication_intents == (intent,)
        assert len(projected.obligations) == 1
        assert projected.obligations[0].lifecycle.declared_name == "publishing"
        with pytest.raises(PublicationUncertain):
            db.attempts.fail_unknown(fence, expected_pointer_revision=1)
        with pytest.raises(PublicationUncertain):
            db.attempts.settle_nonpublication(
                fence,
                expected_pointer_revision=1,
                outcome=AttemptFailedAttempt(),
                reason_code=GivenReasonCode("no_receipt"),
            )
        assert db.snapshots.get("exec").is_current
        assert db.snapshots.get("exec").attempt is not None
        assert not db.snapshots.get("exec").attempt.lifecycle.backend_done
        assert db.snapshots.get("exec").publication_evidence == ()
    with store(db_path) as reopened:
        obligations = reopened.snapshots.get("exec").obligations
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
        assert reopened.snapshots.get("exec").is_current
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
        assert reopened.snapshots.get("exec").attempt is not None
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
        assert db.participants.get("owner").owner_identity.generation == 1
        assert (
            db.session._connection.execute("SELECT count(*) FROM participants").fetchone()[0] == 1
        )


def _read_while_other_store_waits(
    path: Path,
    first_table: str,
    read: Callable[[StandaloneCoordination], object],
    write: Callable[[StandaloneCoordination], None],
) -> object:
    """Hold a reader while a second connection waits for write claim_scope."""
    first_read = threading.Event()
    writer_ready = threading.Event()
    write_started = threading.Event()
    writer_done = threading.Event()
    errors: list[BaseException] = []

    class PausedReader(StandaloneCoordination):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            paused = False
            selected = False

            def trace(statement):
                nonlocal selected
                selected = f"FROM {first_table} " in statement or (
                    f'FROM "{first_table}" ' in statement
                )

            def read_row(cursor, values):
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
        lambda reader: reader.snapshots.get("exec"),
        lambda writer: writer.attempts.settle_nonpublication(
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
        assert type(db.snapshots.get("exec").execution.lifecycle) is FailedExecution


def test_public_participant_is_one_committed_revision_during_rename(db_path: Path) -> None:
    with store(db_path) as db:
        db.participants.register("owner", "Old", "thread", committed=True)

    observed = _read_while_other_store_waits(
        db_path,
        "participants",
        lambda reader: reader.participants.get("owner"),
        lambda writer: writer.participants.rename("owner", "New", "New", expected_generation=1),
    )
    assert observed.display_name == "Old"
    assert observed.aliases == ("owner",)
    with store(db_path) as db:
        assert db.participants.get("owner").display_name == "New"
        assert db.participants.get("owner").aliases == ("owner", "New")


def test_public_read_exception_releases_transaction_and_mutators_reuse_it(db_path: Path) -> None:
    with ready(db_path) as db:
        with pytest.raises(IdentityConflict):
            db.snapshots.get("missing")
        assert not db.session._connection.in_transaction
        with pytest.raises(IdentityConflict):
            db.participants.get("missing")
        assert not db.session._connection.in_transaction
        with db.session.writing():
            assert db.session._connection.in_transaction
            assert type(db.snapshots.get("exec").execution.lifecycle) is PendingExecution
            assert db.participants.get("owner").committed
            assert db.session._connection.in_transaction
        assert not db.session._connection.in_transaction
        db.participants.register("other", "Other", "other-thread")
        assert db.participants.get("other").display_name == "Other"


def test_observation_closes_failed_reads_and_preserves_schema_disposition(db_path: Path) -> None:
    from agent_comms.coordination_database import CoordinationStore

    with store(db_path) as writer:
        writer.participants.register("owner", "Owner", "thread", committed=True)
    for statement in (
        "UPDATE participants SET display_name='unauthorized'",
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
        assert writer.participants.get("owner").display_name == "Owner"


@pytest.mark.parametrize(
    "invalid",
    ["false", 0, 1, None, PromptStartingAttempt(60000)],
    ids=["string", "zero", "one", "none", "live-phase"],
)
def test_nonterminal_settlement_cannot_silence_wire(db_path: Path, invalid: object) -> None:
    with ready(db_path) as db:
        _, fence = started(db)
        fence = final_evidence(db, fence)
        before = db.snapshots.get("exec")
        with pytest.raises(ValueError):
            db.attempts.settle_nonpublication(fence, expected_pointer_revision=1, outcome=invalid)
        assert db.snapshots.get("exec") == before
        assert not db.session._connection.in_transaction


@pytest.mark.parametrize("invalid", ["false", 0, 1, None], ids=["string", "zero", "one", "none"])
def test_other_public_evidence_flags_require_exact_bool(db_path: Path, invalid: object) -> None:
    with store(db_path) as db:
        with pytest.raises(ValueError):
            db.participants.register("owner", "Owner", "thread", committed=invalid)
        assert (
            db.session._connection.execute("SELECT count(*) FROM participants").fetchone()[0] == 0
        )
    with ready(db_path) as db:
        _, fence = started(db)
        before = db.snapshots.get("exec")
        for field in ("backend_done", "process_dead", "progress"):
            with pytest.raises(ValueError):
                db.attempts.advance(
                    fence,
                    PromptAcceptedAttempt,
                    expected_pointer_revision=1,
                    **{field: invalid},
                )
            assert db.snapshots.get("exec") == before
        for field in ("replay_safe", "side_effects_possible"):
            flags = {"replay_safe": True, "side_effects_possible": False}
            flags[field] = invalid
            with pytest.raises(ValueError):
                db.attempts.observe_replay(
                    fence,
                    expected_pointer_revision=1,
                    expected_replay_revision=None,
                    facts=ReplayFact.NONE,
                    **flags,
                )
            assert db.snapshots.get("exec") == before
        assert not db.session._connection.in_transaction
