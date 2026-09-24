"""Slice-2 nonpublication mutation gates against the frozen schema."""

from __future__ import annotations

import hashlib
import multiprocessing
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

import agent_comms.coordination_store as store_module
from agent_comms.coordination import (
    ACPClientConnectivity,
    AttemptPhase,
    ClaimDisposition,
    ExecutionOrigin,
    ExecutionStatus,
    MessageAudience,
    OwnerConnectivity,
    PublicationIntent,
    RecoveryKind,
    ReplayFact,
    TriageVerdict,
    WakeClaim,
    WakeMode,
    canonical_publication_key,
)
from agent_comms.coordination_store import (
    _MONITOR_GRANT,
    INITIAL_LEASE_DURATION_MS,
    AlreadyApplied,
    Applied,
    IdentityConflict,
    MonitorEvidence,
    MutationStore,
    PublicationActivationBlocked,
    PublicationUncertain,
    RecoveryBlocked,
    RecoveryMonitorCapability,
    StaleFence,
    StaleRevision,
    VerifiedOwnerLoss,
    prepare_fence_token,
)
from agent_comms.declarations import Message, MessageType


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "coordination.sqlite3"


def store(path: Path) -> MutationStore:
    return MutationStore(str(path), clock_ms=lambda: 1_000)


def ready(
    path: Path, *, origin: ExecutionOrigin = ExecutionOrigin.WIRE, max_attempts: int = 2
) -> MutationStore:
    db = store(path)
    assert isinstance(db.register_participant("owner", "Owner", "thread", committed=True), Applied)
    if origin is ExecutionOrigin.WIRE:
        db.accept_claim(
            WakeClaim(
                "claim",
                "Owner",
                "owner",
                1,
                "message",
                None,
                MessageAudience.DIRECT,
                WakeMode.FULL,
                None,
                ClaimDisposition.FULL_PENDING,
                1_000,
                1_000,
            )
        )
    db.create_execution(
        "exec",
        origin,
        "owner",
        "thread",
        max_attempts,
        claim_ids=("claim",) if origin is ExecutionOrigin.WIRE else (),
        exact_target="owner" if origin is ExecutionOrigin.WIRE else None,
    )
    db.mark_pending("exec", expected_revision=1)
    return db


def started(db: MutationStore) -> tuple[str, object]:
    token = prepare_fence_token()
    result = db.start_attempt(
        "exec", 1, "thread", 1, token, expected_execution_revision=2, expected_pointer_revision=0
    )
    assert isinstance(result, Applied)
    return token, result.value.fence


def final_evidence(db: MutationStore, fence: object, pointer_revision: int = 1) -> object:
    from agent_comms.coordination import OwnerFence

    assert isinstance(fence, OwnerFence)
    result = db.advance_attempt(
        fence, AttemptPhase.PROMPT_ACCEPTED, expected_pointer_revision=pointer_revision
    )
    result = db.advance_attempt(
        result.value.fence, AttemptPhase.MODEL_RUNNING, expected_pointer_revision=pointer_revision
    )
    result = db.advance_attempt(
        result.value.fence,
        AttemptPhase.SETTLING,
        expected_pointer_revision=pointer_revision,
        backend_done=True,
        process_dead=True,
    )
    return result.value.fence


def _concurrent_registration(
    path: str, gate: multiprocessing.Event, output: multiprocessing.Queue
) -> None:
    try:
        with MutationStore(path, clock_ms=lambda: 1_000) as db:
            gate.wait(timeout=5)
            result = db.register_participant("owner", "Owner", "thread", committed=True)
            output.put(type(result).__name__)
    except Exception as exc:
        output.put(type(exc).__name__)


def monitor_evidence(
    backend_done: bool,
    *,
    subprocess_dead: bool = True,
    unknown_effects: bool = True,
) -> MonitorEvidence:
    return MonitorEvidence(
        subprocess_dead=subprocess_dead,
        backend_done=backend_done,
        reason_code="process_died",
        observed_at_ms=1_000,
        unknown_effects=unknown_effects,
    )


@pytest.fixture
def test_only_trusted_owner_loss(monkeypatch: pytest.MonkeyPatch) -> VerifiedOwnerLoss:
    # This is an isolated test simulation of a FUTURE verifier, NOT a production
    # issuer.  Production _owner_loss_verified always fails closed in Slice 2.
    proof = object.__new__(VerifiedOwnerLoss)
    object.__setattr__(proof, "execution_id", "exec")
    object.__setattr__(proof, "owner_lookup", "owner")
    object.__setattr__(proof, "owner_generation", 1)
    object.__setattr__(proof, "attempt_ordinal", 1)
    monkeypatch.setattr(
        store_module,
        "_owner_loss_verified",
        lambda supplied, execution_id, lookup, generation, ordinal: (
            supplied is proof
            and (execution_id, lookup, generation, ordinal)
            == (
                proof.execution_id,
                proof.owner_lookup,
                proof.owner_generation,
                proof.attempt_ordinal,
            )
        ),
    )
    return proof


def _concurrent_start(
    path: str, token: str, gate: multiprocessing.Event, output: multiprocessing.Queue
) -> None:
    try:
        with MutationStore(path, clock_ms=lambda: 1_000) as db:
            gate.wait(timeout=5)
            result = db.start_attempt(
                "exec",
                1,
                "thread",
                1,
                token,
                expected_execution_revision=2,
                expected_pointer_revision=0,
            )
            output.put(type(result).__name__)
    except Exception as exc:
        output.put(type(exc).__name__)


def test_registration_complete_atomic_reopen_and_rename(db_path: Path) -> None:
    db = store(db_path)
    original = db.register_participant("owner", "Owner", "thread", committed=False)
    assert isinstance(original, Applied)
    assert original.value.pointer.pointer_revision == 0
    assert original.value.aliases == ("owner",)
    assert isinstance(db.register_participant("owner", "Owner", "thread"), AlreadyApplied)
    assert isinstance(db.commit_participant("owner"), Applied)
    assert isinstance(db.commit_participant("owner"), AlreadyApplied)
    rename = db.rename_participant("owner", "Owner New", "Owner New", expected_generation=1)
    assert rename.value.aliases == ("owner", "Owner New")
    assert isinstance(
        db.rename_participant("owner", "Owner New", "Owner New", expected_generation=1),
        AlreadyApplied,
    )
    assert isinstance(
        db.advance_owner_generation("owner", "thread", expected_generation=1), Applied
    )
    with pytest.raises(StaleRevision):
        db.advance_owner_generation("owner", "thread", expected_generation=1)
    db.close()
    with store(db_path) as reopened:
        assert reopened.participant("owner").generation == 2
        assert reopened.participant("owner").committed
        assert reopened.participant("owner").aliases == ("owner", "Owner New")
        replay = reopened.register_participant("owner", "Owner", "thread", committed=False)
        assert isinstance(replay, AlreadyApplied)
        assert replay.value.generation == 2
        assert replay.value.display_name == "Owner New"


def test_registration_transaction_rollback_on_alias_collision(db_path: Path) -> None:
    with store(db_path) as db:
        db.register_participant("first", "First", "thread", alias="alias")
        with pytest.raises(IdentityConflict):
            db.register_participant("second", "Second", "thread", alias="alias")
        assert db._connection.execute("SELECT count(*) FROM participants").fetchone()[0] == 1
        assert not db._connection.in_transaction
    with store(db_path) as db:
        assert db._connection.execute("SELECT count(*) FROM owner_generations").fetchone()[0] == 1
        assert db._connection.execute("SELECT count(*) FROM current_executions").fetchone()[0] == 1


def test_claim_execution_idempotency_and_rollback(db_path: Path) -> None:
    db = ready(db_path)
    snap = db.snapshot("exec")
    assert snap.execution.status is ExecutionStatus.PENDING
    assert snap.claims[0].disposition is ClaimDisposition.ENGAGED
    assert snap.links[0].ordinal == 0
    assert snap.obligation is not None
    replay = db.create_execution(
        "exec",
        ExecutionOrigin.WIRE,
        "owner",
        "thread",
        2,
        claim_ids=("claim",),
        exact_target="owner",
    )
    assert isinstance(replay, AlreadyApplied)
    with pytest.raises(IdentityConflict):
        db.create_execution(
            "exec",
            ExecutionOrigin.WIRE,
            "owner",
            "thread",
            2,
            claim_ids=("claim",),
            exact_target="@different",
        )
    db.close()
    with store(db_path) as reopened:
        assert reopened.snapshot("exec").links == snap.links


def test_claim_acceptance_requires_initial_mode_decision(db_path: Path) -> None:
    with store(db_path) as db:
        db.register_participant("owner", "Owner", "thread", committed=True)

        def claim(
            index: int,
            mode: WakeMode,
            disposition: ClaimDisposition,
            verdict: TriageVerdict | None = None,
        ) -> WakeClaim:
            return WakeClaim(
                f"claim-{index}",
                "Owner",
                "owner",
                index,
                f"message-{index}",
                None,
                MessageAudience.DIRECT,
                mode,
                verdict,
                disposition,
                1_000,
                1_000,
            )

        for index, mode, disposition, verdict in (
            (1, WakeMode.BOUNDED_TRIAGE, ClaimDisposition.IGNORED, TriageVerdict.IGNORE),
            (2, WakeMode.BOUNDED_TRIAGE, ClaimDisposition.DEFERRED, None),
            (3, WakeMode.BOUNDED_TRIAGE, ClaimDisposition.FAILED, None),
            (4, WakeMode.FULL, ClaimDisposition.DEFERRED, None),
            (5, WakeMode.FULL, ClaimDisposition.FAILED, None),
        ):
            with pytest.raises(IdentityConflict):
                db.accept_claim(claim(index, mode, disposition, verdict))
            assert db._connection.execute("SELECT count(*) FROM wake_claims").fetchone()[0] == 0
        for index, mode, disposition in (
            (6, WakeMode.PASSIVE, ClaimDisposition.PASSIVE),
            (7, WakeMode.BOUNDED_TRIAGE, ClaimDisposition.TRIAGE_PENDING),
            (8, WakeMode.FULL, ClaimDisposition.FULL_PENDING),
            (9, WakeMode.FULL, ClaimDisposition.FULL_PENDING),
        ):
            initial = claim(index, mode, disposition)
            assert isinstance(db.accept_claim(initial), Applied)
            assert db.claim(initial.claim_id).revision == 1
        ignored = db.transition_preengagement(
            "claim-7",
            ClaimDisposition.IGNORED,
            expected_revision=1,
            verdict=TriageVerdict.IGNORE,
        )
        deferred = db.transition_preengagement(
            "claim-8", ClaimDisposition.DEFERRED, expected_revision=1
        )
        failed = db.transition_preengagement(
            "claim-9", ClaimDisposition.FAILED, expected_revision=1
        )
        assert ignored.value.revision == deferred.value.revision == failed.value.revision == 2
        assert isinstance(
            db.accept_claim(claim(7, WakeMode.BOUNDED_TRIAGE, ClaimDisposition.TRIAGE_PENDING)),
            AlreadyApplied,
        )
        assert db.claim("claim-7").disposition is ClaimDisposition.IGNORED


def test_claim_preengagement_and_mixed_engagement_rollback(db_path: Path) -> None:
    with store(db_path) as db:
        db.register_participant("owner", "Owner", "thread", committed=True)
        for index, disposition in (
            (1, ClaimDisposition.TRIAGE_PENDING),
            (2, ClaimDisposition.PASSIVE),
        ):
            db.accept_claim(
                WakeClaim(
                    str(index),
                    "Owner",
                    "owner",
                    index,
                    str(index),
                    None,
                    MessageAudience.DIRECT,
                    WakeMode.BOUNDED_TRIAGE if index == 1 else WakeMode.PASSIVE,
                    None,
                    disposition,
                    1_000,
                    1_000,
                )
            )
        deferred = db.transition_preengagement("1", ClaimDisposition.DEFERRED, expected_revision=1)
        assert deferred.value.disposition is ClaimDisposition.DEFERRED
        with pytest.raises(StaleRevision):
            db.transition_preengagement("1", ClaimDisposition.FAILED, expected_revision=1)
        with pytest.raises((IdentityConflict, sqlite3.IntegrityError)):
            db.create_execution(
                "exec",
                ExecutionOrigin.WIRE,
                "owner",
                "thread",
                2,
                claim_ids=("1", "2"),
                exact_target="owner",
            )
        assert db.claim("1").execution_id is None
        assert db._connection.execute("SELECT count(*) FROM executions").fetchone()[0] == 0
        assert not db._connection.in_transaction


def test_token_prepared_before_transaction_and_loss_replay_ignores_mutable(db_path: Path) -> None:
    with ready(db_path) as db:
        token = prepare_fence_token()
        assert len(token) == 64
        assert not db._connection.in_transaction
        first = db.start_attempt(
            "exec",
            1,
            "thread",
            1,
            token,
            expected_execution_revision=2,
            expected_pointer_revision=0,
        )
        assert isinstance(first, Applied)
        attempt = first.value.snapshot.attempt
        assert attempt is not None
        assert attempt.owner_token_digest == hashlib.sha256(token.encode()).hexdigest()
        assert attempt.lease_expires_at_ms == attempt.created_at_ms + INITIAL_LEASE_DURATION_MS
        assert token not in str(first.value.snapshot.to_primitive())
        assert token not in db_path.read_bytes().decode("latin1")
        duplicate = db.start_attempt(
            "exec",
            1,
            "thread",
            1,
            token,
            expected_execution_revision=2,
            expected_pointer_revision=0,
        )
        assert isinstance(duplicate, AlreadyApplied)
        progressed = db.advance_attempt(
            first.value.fence, AttemptPhase.PROMPT_ACCEPTED, expected_pointer_revision=1
        )
        renewed = db.renew_attempt_lease(
            progressed.value.fence, expected_pointer_revision=1, duration_ms=120_000
        )
        assert renewed.value.snapshot.attempt is not None
        assert renewed.value.snapshot.attempt.lease_expires_at_ms >= 121_000
        duplicate = db.start_attempt(
            "exec",
            1,
            "thread",
            1,
            token,
            expected_execution_revision=2,
            expected_pointer_revision=0,
        )
        assert isinstance(duplicate, AlreadyApplied)
        assert duplicate.value.snapshot.attempt == renewed.value.snapshot.attempt
        with pytest.raises(IdentityConflict):
            db.start_attempt(
                "exec",
                2,
                "thread",
                1,
                token,
                expected_execution_revision=2,
                expected_pointer_revision=0,
            )
        with pytest.raises(StaleRevision):
            db.advance_attempt(
                first.value.fence, AttemptPhase.MODEL_RUNNING, expected_pointer_revision=1
            )
    with store(db_path) as reopened:
        duplicate = reopened.start_attempt(
            "exec",
            1,
            "thread",
            1,
            token,
            expected_execution_revision=2,
            expected_pointer_revision=0,
        )
        assert isinstance(duplicate, AlreadyApplied)


def test_token_digest_is_global_and_only_digest_persists(db_path: Path) -> None:
    with ready(db_path) as db:
        token, _ = started(db)
        db.register_participant("other", "Other", "other-thread", committed=True)
        db.create_execution("other-exec", ExecutionOrigin.GOAL, "other", "other-thread", 1)
        db.mark_pending("other-exec", expected_revision=1)
        with pytest.raises(IdentityConflict):
            db.start_attempt(
                "other-exec",
                1,
                "other-thread",
                1,
                token,
                expected_execution_revision=2,
                expected_pointer_revision=0,
            )
        assert db.snapshot("other-exec").attempt is None
        assert (
            db._connection.execute("SELECT owner_token_digest FROM attempts").fetchone()[0]
            == hashlib.sha256(token.encode()).hexdigest()
        )


def test_nonpublication_silent_atomic_settlement(db_path: Path) -> None:
    with ready(db_path) as db:
        _, fence = started(db)
        fence = final_evidence(db, fence)
        settled = db.settle_nonpublication(fence, expected_pointer_revision=1, success=True)
        assert settled.value.execution.status is ExecutionStatus.COMPLETED
        assert settled.value.attempt is not None
        assert settled.value.attempt.phase is AttemptPhase.SUCCEEDED
        assert settled.value.obligation is not None
        assert settled.value.obligation.state.value == "silent"
        assert settled.value.claims[0].disposition is ClaimDisposition.COMPLETED
        assert not settled.value.is_current
        assert settled.value.pointer_revision == 2
    with store(db_path) as reopened:
        assert reopened.snapshot("exec").execution.status is ExecutionStatus.COMPLETED


def test_retry_partition_and_generation_fence(db_path: Path) -> None:
    with ready(db_path) as db:
        token, fence = started(db)
        db.observe_replay(
            fence,
            expected_pointer_revision=1,
            expected_replay_revision=None,
            facts=ReplayFact.NONE,
            replay_safe=True,
            side_effects_possible=False,
        )
        fence = final_evidence(db, fence)
        outcome = db.settle_nonpublication(
            fence, expected_pointer_revision=1, success=False, reason_code="provider_failed"
        )
        assert outcome.value.execution.status is ExecutionStatus.DEFERRED
        assert outcome.value.can_retry
        with pytest.raises(StaleFence):
            db.start_attempt(
                "exec",
                2,
                "thread",
                1,
                prepare_fence_token(),
                expected_execution_revision=4,
                expected_pointer_revision=2,
            )
        db.advance_owner_generation("owner", "thread", expected_generation=1)
        second = db.start_attempt(
            "exec",
            2,
            "thread",
            2,
            prepare_fence_token(),
            expected_execution_revision=4,
            expected_pointer_revision=2,
        )
        assert second.value.snapshot.attempt is not None
        assert second.value.snapshot.attempt.attempt_ordinal == 2
        with pytest.raises(StaleFence):
            db.advance_attempt(fence, AttemptPhase.MODEL_RUNNING, expected_pointer_revision=3)
        assert isinstance(
            db.start_attempt(
                "exec",
                1,
                "thread",
                1,
                token,
                expected_execution_revision=2,
                expected_pointer_revision=0,
            ),
            AlreadyApplied,
        )


@pytest.mark.parametrize("first_fact", ["process_dead", "backend_done"])
def test_final_backend_fact_blocks_later_phase_progress_and_dead_lease(
    db_path: Path, first_fact: str
) -> None:
    with ready(db_path) as db:
        _, fence = started(db)
        first = db.advance_attempt(
            fence,
            AttemptPhase.PROMPT_STARTING,
            expected_pointer_revision=1,
            **{first_fact: True},
        )
        fence = first.value.fence
        before = db.snapshot("exec")
        for phase, progress in (
            (AttemptPhase.PROMPT_ACCEPTED, False),
            (AttemptPhase.PROMPT_STARTING, True),
            (AttemptPhase.PROMPT_STARTING, False),
        ):
            with pytest.raises(RecoveryBlocked):
                db.advance_attempt(fence, phase, expected_pointer_revision=1, progress=progress)
            assert db.snapshot("exec") == before
        if first_fact == "process_dead":
            with pytest.raises(RecoveryBlocked):
                db.renew_attempt_lease(fence, expected_pointer_revision=1, duration_ms=1_000)
            assert db.snapshot("exec") == before
            second_fact = "backend_done"
        else:
            # A done-but-live child may still be waiting for verified exit.
            renewed = db.renew_attempt_lease(fence, expected_pointer_revision=1, duration_ms=1_000)
            fence = renewed.value.fence
            second_fact = "process_dead"
        final = db.advance_attempt(
            fence,
            AttemptPhase.PROMPT_STARTING,
            expected_pointer_revision=1,
            **{second_fact: True},
        )
        fence = final.value.fence
        assert final.value.snapshot.attempt is not None
        assert final.value.snapshot.attempt.backend_done
        assert final.value.snapshot.attempt.process_dead
        with pytest.raises(RecoveryBlocked):
            db.advance_attempt(fence, AttemptPhase.PROMPT_STARTING, expected_pointer_revision=1)
        outcome = db.settle_nonpublication(
            fence, expected_pointer_revision=1, success=False, reason_code="failed"
        )
        assert outcome.value.execution.status is ExecutionStatus.FAILED


def test_replay_safety_does_not_cross_retry_attempts(db_path: Path) -> None:
    with ready(db_path, max_attempts=3) as db:
        _, first = started(db)
        initial = db.observe_replay(
            first,
            expected_pointer_revision=1,
            expected_replay_revision=None,
            facts=ReplayFact.NONE,
            replay_safe=True,
            side_effects_possible=False,
        )
        assert initial.value.replay is not None
        same = db.observe_replay(
            first,
            expected_pointer_revision=1,
            expected_replay_revision=1,
            facts=ReplayFact.NONE,
            replay_safe=True,
            side_effects_possible=False,
        )
        assert isinstance(same, AlreadyApplied)
        first = final_evidence(db, first)
        deferred = db.settle_nonpublication(
            first, expected_pointer_revision=1, success=False, reason_code="first_failed"
        )
        assert deferred.value.can_retry
        db.advance_owner_generation("owner", "thread", expected_generation=1)
        second_token = prepare_fence_token()
        second = db.start_attempt(
            "exec",
            2,
            "thread",
            2,
            second_token,
            expected_execution_revision=4,
            expected_pointer_revision=2,
        )
        assert second.value.snapshot.replay is not None
        assert not second.value.snapshot.replay.replay_safe
        assert second.value.snapshot.replay.revision == 2
        replayed_start = db.start_attempt(
            "exec",
            2,
            "thread",
            2,
            second_token,
            expected_execution_revision=4,
            expected_pointer_revision=2,
        )
        assert isinstance(replayed_start, AlreadyApplied)
        assert db.snapshot("exec").replay == second.value.snapshot.replay
        same_unsafe = db.observe_replay(
            second.value.fence,
            expected_pointer_revision=3,
            expected_replay_revision=2,
            facts=ReplayFact.NONE,
            replay_safe=False,
            side_effects_possible=False,
        )
        assert isinstance(same_unsafe, AlreadyApplied)
        with pytest.raises(IdentityConflict):
            db.observe_replay(
                second.value.fence,
                expected_pointer_revision=3,
                expected_replay_revision=2,
                facts=ReplayFact.NONE,
                replay_safe=True,
                side_effects_possible=False,
            )
        second_fence = final_evidence(db, second.value.fence, pointer_revision=3)
        outcome = db.settle_nonpublication(
            second_fence, expected_pointer_revision=3, success=False, reason_code="second_failed"
        )
        assert outcome.value.execution.status is ExecutionStatus.FAILED
        assert not outcome.value.can_retry
        assert outcome.value.replay is not None and not outcome.value.replay.replay_safe
        with pytest.raises((StaleFence, IdentityConflict)):
            db.start_attempt(
                "exec",
                3,
                "thread",
                3,
                prepare_fence_token(),
                expected_execution_revision=6,
                expected_pointer_revision=4,
            )


def test_retry_start_rollback_restores_prior_replay_authorization(db_path: Path) -> None:
    with ready(db_path, max_attempts=3) as db:
        _, first = started(db)
        db.observe_replay(
            first,
            expected_pointer_revision=1,
            expected_replay_revision=None,
            facts=ReplayFact.NONE,
            replay_safe=True,
            side_effects_possible=False,
        )
        first = final_evidence(db, first)
        deferred = db.settle_nonpublication(
            first, expected_pointer_revision=1, success=False, reason_code="first_failed"
        )
        assert deferred.value.can_retry
        db.advance_owner_generation("owner", "thread", expected_generation=1)
        before = db.snapshot("exec")
        prepared = prepare_fence_token()
        db._connection.execute(
            "CREATE TEMP TRIGGER fail_retry_obligation BEFORE UPDATE OF state "
            "ON main.obligations WHEN NEW.state='pending' BEGIN "
            "SELECT RAISE(ABORT, 'injected retry failure'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected retry failure"):
            db.start_attempt(
                "exec",
                2,
                "thread",
                2,
                prepared,
                expected_execution_revision=4,
                expected_pointer_revision=2,
            )
        assert db.snapshot("exec") == before
        assert db._connection.execute("SELECT count(*) FROM attempts").fetchone()[0] == 1
        assert not db._connection.in_transaction
        db._connection.execute("DROP TRIGGER fail_retry_obligation")
        second = db.start_attempt(
            "exec",
            2,
            "thread",
            2,
            prepared,
            expected_execution_revision=4,
            expected_pointer_revision=2,
        )
        assert isinstance(second, Applied)
        assert second.value.snapshot.replay is not None
        assert not second.value.snapshot.replay.replay_safe
        assert second.value.snapshot.replay.revision == 2


def test_ambiguity_forces_failed_partition(db_path: Path) -> None:
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
        fence = final_evidence(db, fence)
        settled = db.settle_nonpublication(fence, expected_pointer_revision=1, success=False)
        assert settled.value.execution.status is ExecutionStatus.FAILED
        assert not settled.value.can_retry
        assert settled.value.replay is not None
        assert settled.value.replay.facts == ReplayFact.TOOL_EXECUTED


def test_fenced_connectivity_audit_and_monotone_replay(db_path: Path) -> None:
    with ready(db_path) as db:
        _, fence = started(db)
        observed = db.observe_connectivity(
            fence,
            expected_pointer_revision=1,
            expected_revision=None,
            owner=OwnerConnectivity.RECONNECTING,
            acp_client=ACPClientConnectivity.DISCONNECTED,
        )
        assert observed.value.connectivity is not None
        assert observed.value.connectivity.revision == 1
        with pytest.raises(StaleRevision):
            db.observe_connectivity(
                fence,
                expected_pointer_revision=1,
                expected_revision=None,
                owner=OwnerConnectivity.OFFLINE,
                acp_client=ACPClientConnectivity.DISCONNECTED,
            )
        with pytest.raises(IdentityConflict):
            db.append_recovery_audit(
                fence, expected_pointer_revision=1, kind=RecoveryKind.FAILED, reason_code="invented"
            )
        accepted = db.advance_attempt(
            fence, AttemptPhase.PROMPT_ACCEPTED, expected_pointer_revision=1
        )
        stalled = db.advance_attempt(
            accepted.value.fence, AttemptPhase.MODEL_STALLED, expected_pointer_revision=1
        )
        fence = stalled.value.fence
        audited = db.append_recovery_audit(
            fence,
            expected_pointer_revision=1,
            kind=RecoveryKind.MODEL_STALLED,
            reason_code="stalled",
            sanitized_detail="watchdog",
            elapsed_ms=1_000,
        )
        assert audited.value.last_recovery is not None
        assert audited.value.last_recovery.attempt == 1
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
        assert db.snapshot("exec").replay.facts == ReplayFact.TOOL_EXECUTED


def test_recovered_audit_requires_same_attempt_new_incident_and_resumption(db_path: Path) -> None:
    with ready(db_path) as db:
        _, fence = started(db)
        accepted = db.advance_attempt(
            fence, AttemptPhase.PROMPT_ACCEPTED, expected_pointer_revision=1
        )
        stalled = db.advance_attempt(
            accepted.value.fence, AttemptPhase.MODEL_STALLED, expected_pointer_revision=1
        )
        fence = stalled.value.fence
        db.append_recovery_audit(
            fence,
            expected_pointer_revision=1,
            kind=RecoveryKind.MODEL_STALLED,
            reason_code="stalled",
        )
        with pytest.raises(IdentityConflict):
            db.append_recovery_audit(
                fence,
                expected_pointer_revision=1,
                kind=RecoveryKind.RECOVERED,
                reason_code="premature",
            )
        aborting = db.advance_attempt(fence, AttemptPhase.ABORTING, expected_pointer_revision=1)
        with pytest.raises(IdentityConflict):
            db.append_recovery_audit(
                aborting.value.fence,
                expected_pointer_revision=1,
                kind=RecoveryKind.RECOVERED,
                reason_code="still_aborting",
            )
        retrying = db.advance_attempt(
            aborting.value.fence, AttemptPhase.RETRYING, expected_pointer_revision=1
        )
        running = db.advance_attempt(
            retrying.value.fence, AttemptPhase.MODEL_RUNNING, expected_pointer_revision=1
        )
        fence = running.value.fence
        db.append_recovery_audit(
            fence,
            expected_pointer_revision=1,
            kind=RecoveryKind.RECOVERED,
            reason_code="resumed",
        )
        with pytest.raises(IdentityConflict):
            db.append_recovery_audit(
                fence,
                expected_pointer_revision=1,
                kind=RecoveryKind.RECOVERED,
                reason_code="duplicate",
            )
        again = db.advance_attempt(fence, AttemptPhase.MODEL_STALLED, expected_pointer_revision=1)
        db.append_recovery_audit(
            again.value.fence,
            expected_pointer_revision=1,
            kind=RecoveryKind.MODEL_STALLED,
            reason_code="stalled_again",
        )
        aborting = db.advance_attempt(
            again.value.fence, AttemptPhase.ABORTING, expected_pointer_revision=1
        )
        retrying = db.advance_attempt(
            aborting.value.fence, AttemptPhase.RETRYING, expected_pointer_revision=1
        )
        running = db.advance_attempt(
            retrying.value.fence, AttemptPhase.MODEL_RUNNING, expected_pointer_revision=1
        )
        db.append_recovery_audit(
            running.value.fence,
            expected_pointer_revision=1,
            kind=RecoveryKind.RECOVERED,
            reason_code="resumed_again",
        )


def test_recovered_audit_rejects_prior_attempt_incident_and_terminal(db_path: Path) -> None:
    with ready(db_path) as db:
        _, first = started(db)
        accepted = db.advance_attempt(
            first, AttemptPhase.PROMPT_ACCEPTED, expected_pointer_revision=1
        )
        stalled = db.advance_attempt(
            accepted.value.fence, AttemptPhase.MODEL_STALLED, expected_pointer_revision=1
        )
        db.append_recovery_audit(
            stalled.value.fence,
            expected_pointer_revision=1,
            kind=RecoveryKind.MODEL_STALLED,
            reason_code="stalled",
        )
        db.observe_replay(
            stalled.value.fence,
            expected_pointer_revision=1,
            expected_replay_revision=None,
            facts=ReplayFact.NONE,
            replay_safe=True,
            side_effects_possible=False,
        )
        done = db.advance_attempt(
            stalled.value.fence,
            AttemptPhase.MODEL_STALLED,
            expected_pointer_revision=1,
            backend_done=True,
            process_dead=True,
        )
        deferred = db.settle_nonpublication(
            done.value.fence,
            expected_pointer_revision=1,
            success=False,
            reason_code="first_failed",
        )
        assert deferred.value.can_retry
        with pytest.raises(StaleFence):
            db.append_recovery_audit(
                done.value.fence,
                expected_pointer_revision=2,
                kind=RecoveryKind.RECOVERED,
                reason_code="terminal",
            )
        db.advance_owner_generation("owner", "thread", expected_generation=1)
        second = db.start_attempt(
            "exec",
            2,
            "thread",
            2,
            prepare_fence_token(),
            expected_execution_revision=4,
            expected_pointer_revision=2,
        )
        accepted = db.advance_attempt(
            second.value.fence, AttemptPhase.PROMPT_ACCEPTED, expected_pointer_revision=3
        )
        running = db.advance_attempt(
            accepted.value.fence, AttemptPhase.MODEL_RUNNING, expected_pointer_revision=3
        )
        with pytest.raises(IdentityConflict):
            db.append_recovery_audit(
                running.value.fence,
                expected_pointer_revision=3,
                kind=RecoveryKind.RECOVERED,
                reason_code="prior_attempt",
            )


def test_monitor_requires_owner_loss_separate_from_child_exit(
    db_path: Path, test_only_trusted_owner_loss: VerifiedOwnerLoss
) -> None:
    with ready(db_path) as db:
        _, fence = started(db)
        monitor = RecoveryMonitorCapability(db, _grant=_MONITOR_GRANT)
        for evidence, owner_loss in (
            (monitor_evidence(True), None),
            (monitor_evidence(True, subprocess_dead=False), test_only_trusted_owner_loss),
            (monitor_evidence(True), object.__new__(VerifiedOwnerLoss)),
        ):
            with pytest.raises(RecoveryBlocked):
                monitor.terminalize_dead_attempt(
                    "exec",
                    1,
                    1,
                    expected_attempt_revision=1,
                    expected_execution_revision=3,
                    expected_pointer_revision=1,
                    evidence=evidence,
                    owner_loss=owner_loss,
                )
            snapshot = db.snapshot("exec")
            assert snapshot.is_current
            assert snapshot.attempt is not None
            assert snapshot.attempt.revision == fence.revision
            assert not snapshot.attempt.process_dead
            assert not snapshot.attempt.backend_done
            assert snapshot.replay is None


def test_monitor_production_verifier_remains_closed(db_path: Path) -> None:
    with ready(db_path) as db:
        _, fence = started(db)
        with pytest.raises(RecoveryBlocked):
            VerifiedOwnerLoss(execution_id="exec")
        monitor = RecoveryMonitorCapability(db, _grant=_MONITOR_GRANT)
        with pytest.raises(RecoveryBlocked):
            monitor.terminalize_dead_attempt(
                "exec",
                1,
                1,
                expected_attempt_revision=1,
                expected_execution_revision=3,
                expected_pointer_revision=1,
                evidence=monitor_evidence(True),
                owner_loss=object.__new__(VerifiedOwnerLoss),
            )
        snapshot = db.snapshot("exec")
        assert snapshot.is_current
        assert snapshot.attempt is not None and snapshot.attempt.revision == fence.revision
        assert not snapshot.attempt.process_dead and not snapshot.attempt.backend_done
        assert snapshot.replay is None


def test_monitor_death_without_done_is_blocked_and_keeps_pointer(
    db_path: Path, test_only_trusted_owner_loss: VerifiedOwnerLoss
) -> None:
    with ready(db_path) as db:
        _, fence = started(db)
        with pytest.raises(PermissionError):
            RecoveryMonitorCapability(db, _grant=object())
        monitor = RecoveryMonitorCapability(db, _grant=_MONITOR_GRANT)
        with pytest.raises(RecoveryBlocked):
            monitor.terminalize_dead_attempt(
                "exec",
                1,
                1,
                expected_attempt_revision=1,
                expected_execution_revision=3,
                expected_pointer_revision=1,
                evidence=monitor_evidence(False),
                owner_loss=test_only_trusted_owner_loss,
            )
        persisted = db.snapshot("exec")
        assert persisted.is_current
        assert persisted.attempt is not None and persisted.attempt.process_dead
        assert not persisted.attempt.backend_done
        assert persisted.replay is not None and persisted.replay.facts == ReplayFact.UNKNOWN_EFFECTS
        with pytest.raises(StaleRevision):
            monitor.terminalize_dead_attempt(
                "exec",
                1,
                1,
                expected_attempt_revision=1,
                expected_execution_revision=3,
                expected_pointer_revision=1,
                evidence=monitor_evidence(True),
                owner_loss=test_only_trusted_owner_loss,
            )
        after = monitor.terminalize_dead_attempt(
            "exec",
            1,
            1,
            expected_attempt_revision=2,
            expected_execution_revision=3,
            expected_pointer_revision=1,
            evidence=monitor_evidence(True),
            owner_loss=test_only_trusted_owner_loss,
        )
        assert after.value.execution.status is ExecutionStatus.FAILED
        assert after.value.last_recovery is not None
        assert after.value.last_recovery.attempt == 1


def test_monitor_known_safe_evidence_allows_deferred_and_never_publishes(
    db_path: Path, test_only_trusted_owner_loss: VerifiedOwnerLoss
) -> None:
    with ready(db_path) as db:
        _, fence = started(db)
        db.observe_replay(
            fence,
            expected_pointer_revision=1,
            expected_replay_revision=None,
            facts=ReplayFact.NONE,
            replay_safe=True,
            side_effects_possible=False,
        )
        monitor = RecoveryMonitorCapability(db, _grant=_MONITOR_GRANT)
        outcome = monitor.terminalize_dead_attempt(
            "exec",
            1,
            1,
            expected_attempt_revision=1,
            expected_execution_revision=3,
            expected_pointer_revision=1,
            evidence=monitor_evidence(True, unknown_effects=False),
            owner_loss=test_only_trusted_owner_loss,
        )
        assert outcome.value.execution.status is ExecutionStatus.DEFERRED
        assert outcome.value.publication_intent is None
        assert outcome.value.publication_receipt is None
        assert not hasattr(db, "freeze_publication_intent")
        assert not hasattr(db, "record_publication_receipt")
        assert issubclass(PublicationActivationBlocked, Exception)


def test_unstarted_failure_and_claim_requeue(db_path: Path) -> None:
    with ready(db_path) as db:
        failed = db.fail_unstarted("exec", expected_revision=2, reason_code="unavailable")
        assert failed.value.execution.status is ExecutionStatus.FAILED
        assert failed.value.attempt is None
        assert failed.value.claims[0].disposition is ClaimDisposition.FAILED
        assert failed.value.obligation is not None
        assert failed.value.obligation.state.value == "failed"
        with pytest.raises(IdentityConflict):
            db.start_attempt(
                "exec",
                1,
                "thread",
                1,
                prepare_fence_token(),
                expected_execution_revision=3,
                expected_pointer_revision=0,
            )
    with store(db_path) as reopened:
        assert reopened.snapshot("exec").execution.status is ExecutionStatus.FAILED

    with store(db_path.with_name("claim-only.sqlite3")) as db:
        db.register_participant("owner", "Owner", "thread", committed=True)
        db.accept_claim(
            WakeClaim(
                "claim",
                "Owner",
                "owner",
                1,
                "message",
                None,
                MessageAudience.DIRECT,
                WakeMode.FULL,
                None,
                ClaimDisposition.FULL_PENDING,
                1_000,
                1_000,
            )
        )
        deferred = db.transition_preengagement(
            "claim", ClaimDisposition.DEFERRED, expected_revision=1
        )
        assert deferred.value.execution_id is None
        requeued = db.transition_preengagement(
            "claim", ClaimDisposition.FULL_PENDING, expected_revision=2
        )
        assert requeued.value.disposition is ClaimDisposition.FULL_PENDING


def test_frozen_v2_pre_attempt_deferred_execution_cannot_resume(db_path: Path) -> None:
    with ready(db_path) as db:
        # Direct SQL establishes a v2-valid parked state that the typed layer
        # deliberately does not expose: ordinal 1 cannot start from DEFERRED.
        with db._transaction() as connection:
            connection.execute(
                "UPDATE executions SET status='deferred',revision=revision+1 "
                "WHERE execution_id='exec'"
            )
            connection.execute(
                "UPDATE obligations SET state='deferred',revision=revision+1 "
                "WHERE execution_id='exec'"
            )
            connection.execute(
                "UPDATE wake_claims SET disposition='deferred',revision=revision+1 "
                "WHERE claim_id='claim'"
            )
        before = db.snapshot("exec")
        assert before.execution.status is ExecutionStatus.DEFERRED
        assert before.attempt is None
        with pytest.raises(RecoveryBlocked, match="cannot resume an unstarted deferral"):
            db.start_attempt(
                "exec",
                1,
                "thread",
                1,
                prepare_fence_token(),
                expected_execution_revision=3,
                expected_pointer_revision=0,
            )
        assert db.snapshot("exec") == before
    with store(db_path) as reopened:
        assert reopened.snapshot("exec").execution.status is ExecutionStatus.DEFERRED
        assert reopened._connection.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0


def test_crash_reopen_pending_starts_once_after_owner_returns(db_path: Path) -> None:
    db = ready(db_path)
    before = db.snapshot("exec")
    assert before.execution.status is ExecutionStatus.PENDING
    assert before.attempt is None
    db.close()  # Owner disappeared before the attempt began; PENDING remains durable.
    with store(db_path) as returned:
        assert returned.snapshot("exec").execution.status is ExecutionStatus.PENDING
        returned.advance_owner_generation("owner", "thread", expected_generation=1)
        token = prepare_fence_token()
        applied = returned.start_attempt(
            "exec",
            1,
            "thread",
            2,
            token,
            expected_execution_revision=2,
            expected_pointer_revision=0,
        )
        assert isinstance(applied, Applied)
    # Simulate a lost commit response: the owner has retained the secret but
    # no in-memory result, and must not create a second attempt.
    with store(db_path) as reopened:
        replay = reopened.start_attempt(
            "exec",
            1,
            "thread",
            2,
            token,
            expected_execution_revision=2,
            expected_pointer_revision=0,
        )
        assert isinstance(replay, AlreadyApplied)
        assert replay.value.snapshot.is_current
        with pytest.raises(StaleRevision):
            reopened.start_attempt(
                "exec",
                1,
                "thread",
                2,
                prepare_fence_token(),
                expected_execution_revision=2,
                expected_pointer_revision=0,
            )
        assert reopened._connection.execute("SELECT count(*) FROM attempts").fetchone()[0] == 1


def test_frozen_publishing_snapshot_no_nonpublication_settlement(
    db_path: Path, test_only_trusted_owner_loss: VerifiedOwnerLoss
) -> None:
    with ready(db_path) as db:
        _, fence = started(db)
        message = Message(
            sender="thread",
            target="owner",
            body="reply",
            type=MessageType.INFO,
            timestamp=1.0,
            notice=False,
        )
        intent = PublicationIntent(
            "exec",
            "thread",
            "owner",
            MessageType.INFO,
            False,
            1.0,
            "reply",
            hashlib.sha256(b"reply").hexdigest(),
            canonical_publication_key("exec", "owner"),
            message.message_id,
        )
        with db._transaction() as connection:
            connection.execute(
                "INSERT INTO publication_intents VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    intent.execution_id,
                    intent.sender,
                    intent.exact_target,
                    intent.message_type.value,
                    int(intent.notice),
                    intent.timestamp,
                    intent.payload,
                    intent.payload_digest,
                    intent.publication_key,
                    intent.expected_message_id,
                ),
            )
            connection.execute(
                "UPDATE obligations SET state='publishing',revision=revision+1 "
                "WHERE execution_id='exec'"
            )
        projected = db.snapshot("exec")
        assert projected.publication_intent == intent
        assert projected.obligation is not None
        assert projected.obligation.state.value == "publishing"
        with pytest.raises(PublicationUncertain):
            db.settle_nonpublication(
                fence,
                expected_pointer_revision=1,
                success=False,
                reason_code="no_receipt",
            )
        monitor = RecoveryMonitorCapability(db, _grant=_MONITOR_GRANT)
        with pytest.raises(PublicationUncertain):
            monitor.terminalize_dead_attempt(
                "exec",
                1,
                1,
                expected_attempt_revision=1,
                expected_execution_revision=3,
                expected_pointer_revision=1,
                evidence=monitor_evidence(True),
                owner_loss=test_only_trusted_owner_loss,
            )
        assert db.snapshot("exec").is_current
        assert db.snapshot("exec").attempt is not None
        assert db.snapshot("exec").attempt.backend_done
        assert db.snapshot("exec").publication_receipt is None
    with store(db_path) as reopened:
        assert reopened.snapshot("exec").obligation.state.value == "publishing"


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
        assert reopened.snapshot("exec").is_current
        assert reopened._connection.execute("SELECT count(*) FROM attempts").fetchone()[0] == 1


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
        assert reopened.snapshot("exec").attempt is not None
        assert reopened._connection.execute("SELECT count(*) FROM attempts").fetchone()[0] == 1


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
        assert db.participant("owner").generation == 1
        assert db._connection.execute("SELECT count(*) FROM participants").fetchone()[0] == 1


def _read_while_other_store_commits(
    path: Path,
    first_table: str,
    read: Callable[[MutationStore], object],
    write: Callable[[MutationStore], None],
) -> object:
    """Hold a reader after its first SELECT while a second connection tries COMMIT."""
    first_read = threading.Event()
    writer_ready = threading.Event()
    commit_started = threading.Event()
    writer_done = threading.Event()
    errors: list[BaseException] = []

    class PausedReader(MutationStore):
        paused = False

        def _row(self, table: str, column: str, key: object) -> sqlite3.Row | None:
            result = super()._row(table, column, key)
            if table == first_table and not self.paused:
                self.paused = True
                first_read.set()
                assert commit_started.wait(timeout=5), "writer never reached COMMIT"
                # Without one read transaction the writer commits before the
                # remaining SELECTs, producing an impossible mixed aggregate.
                # With rollback-journal read locking it waits until we return.
                writer_done.wait(timeout=0.3)
            return result

    def writer() -> None:
        try:
            with store(path) as other:

                def trace(statement: str) -> None:
                    if statement == "COMMIT":
                        commit_started.set()

                other._connection.set_trace_callback(trace)
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

    observed = _read_while_other_store_commits(
        db_path,
        "executions",
        lambda reader: reader.snapshot("exec"),
        lambda writer: writer.settle_nonpublication(
            fence, expected_pointer_revision=1, success=False, reason_code="failure"
        ),
    )
    assert observed.execution.status is ExecutionStatus.ACTIVE
    assert observed.attempt is not None
    assert observed.attempt.phase is AttemptPhase.SETTLING
    assert observed.is_current
    with store(db_path) as db:
        assert db.snapshot("exec").execution.status is ExecutionStatus.FAILED


def test_public_participant_is_one_committed_revision_during_rename(db_path: Path) -> None:
    with store(db_path) as db:
        db.register_participant("owner", "Old", "thread", committed=True)

    observed = _read_while_other_store_commits(
        db_path,
        "participants",
        lambda reader: reader.participant("owner"),
        lambda writer: writer.rename_participant("owner", "New", "New", expected_generation=1),
    )
    assert observed.display_name == "Old"
    assert observed.aliases == ("owner",)
    with store(db_path) as db:
        assert db.participant("owner").display_name == "New"
        assert db.participant("owner").aliases == ("owner", "New")


def test_public_read_exception_releases_transaction_and_mutators_reuse_it(db_path: Path) -> None:
    with ready(db_path) as db:
        with pytest.raises(IdentityConflict):
            db.snapshot("missing")
        assert not db._connection.in_transaction
        with pytest.raises(IdentityConflict):
            db.participant("missing")
        assert not db._connection.in_transaction
        with db._transaction():
            assert db._connection.in_transaction
            assert db.snapshot("exec").execution.status is ExecutionStatus.PENDING
            assert db.participant("owner").committed
            assert db._connection.in_transaction
        assert not db._connection.in_transaction
        db.register_participant("other", "Other", "other-thread")
        assert db.participant("other").display_name == "Other"


@pytest.mark.parametrize("invalid", ["false", 0, 1, None], ids=["string", "zero", "one", "none"])
def test_non_boolean_settlement_cannot_silence_wire(db_path: Path, invalid: object) -> None:
    with ready(db_path) as db:
        _, fence = started(db)
        fence = final_evidence(db, fence)
        before = db.snapshot("exec")
        with pytest.raises(ValueError, match="boolean"):
            db.settle_nonpublication(fence, expected_pointer_revision=1, success=invalid)
        assert db.snapshot("exec") == before
        assert not db._connection.in_transaction


@pytest.mark.parametrize("invalid", ["false", 0, 1, None], ids=["string", "zero", "one", "none"])
def test_other_public_evidence_flags_require_exact_bool(db_path: Path, invalid: object) -> None:
    with store(db_path) as db:
        with pytest.raises(ValueError, match="boolean"):
            db.register_participant("owner", "Owner", "thread", committed=invalid)
        assert db._connection.execute("SELECT count(*) FROM participants").fetchone()[0] == 0
    with ready(db_path) as db:
        _, fence = started(db)
        before = db.snapshot("exec")
        for field in ("backend_done", "process_dead", "progress"):
            with pytest.raises(ValueError, match="booleans"):
                db.advance_attempt(
                    fence,
                    AttemptPhase.PROMPT_ACCEPTED,
                    expected_pointer_revision=1,
                    **{field: invalid},
                )
            assert db.snapshot("exec") == before
        for field in ("replay_safe", "side_effects_possible"):
            flags = {"replay_safe": True, "side_effects_possible": False}
            flags[field] = invalid
            with pytest.raises(ValueError, match="booleans"):
                db.observe_replay(
                    fence,
                    expected_pointer_revision=1,
                    expected_replay_revision=None,
                    facts=ReplayFact.NONE,
                    **flags,
                )
            assert db.snapshot("exec") == before
        assert not db._connection.in_transaction
