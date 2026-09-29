"""Attempt store: owned coordinator state and transitions."""

from __future__ import annotations

from dataclasses import dataclass, replace

from agent_comms.assignment_states import EngagedAssignment
from agent_comms.attempt_start import AttemptStart
from agent_comms.attempt_states import (
    AttemptFailedAttempt,
    AttemptState,
    PromptStartingAttempt,
    SucceededAttempt,
)
from agent_comms.coordination_contracts import (
    MAX_SANITIZED_DETAIL_CHARS,
    _bounded_reason,
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
from agent_comms.coordination_session import CoordinationSession
from agent_comms.coordination_snapshot import RecoverySnapshot, retry_disposition_authorized
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.coordination_tables.attempts import AttemptRecord, ReplayAssessments, ReplayFact
from agent_comms.coordination_tables.executions import (
    CurrentExecutions,
    ExecutionOrigin,
    ExecutionRecord,
)
from agent_comms.coordination_tables.recovery import (
    ACPClientConnectivity,
    ConnectivityFacet,
    OwnerConnectivity,
    RecoveryAudit,
)
from agent_comms.coordination_tables.responses import ResponseObligation
from agent_comms.execution_states import (
    ActiveExecution,
    CompletedExecution,
    DeferredExecution,
    FailedExecution,
)
from agent_comms.obligation_states import (
    DeferredResponse,
    FailedResponse,
    PendingResponse,
    SilentResponse,
)
from agent_comms.owner_fence import OwnerFence, _digest
from agent_comms.participant_store import ParticipantStore
from agent_comms.recovery_reader import RecoveryReader
from agent_comms.recovery_states import RecoveryCondition


@dataclass(frozen=True, slots=True)
class StartResult:
    snapshot: RecoverySnapshot
    fence: OwnerFence


INITIAL_LEASE_DURATION_MS = 60_000


MAX_LEASE_RENEWAL_MS = 300_000


class AttemptStore:
    def __init__(
        self,
        session: CoordinationSession,
        participants: ParticipantStore,
        snapshots: RecoveryReader,
    ) -> None:
        self.session = session
        self.participants = participants
        self.snapshots = snapshots

    def require_fence(self, fence: OwnerFence) -> tuple[RecoverySnapshot, AttemptRecord]:
        snapshot = self.snapshots.get(fence.execution_id)
        attempt = snapshot.require_current_attempt()
        if (
            attempt.authority != fence.authority
            or self.participants.get(attempt.owner_lookup).owner_identity != attempt.owner_identity
        ):
            raise StaleFence("attempt fence is not current")
        if attempt.revision != fence.revision:
            raise StaleRevision("attempt fence revision changed")
        return snapshot, attempt

    def start(self, request: AttemptStart) -> Applied[StartResult] | AlreadyApplied[StartResult]:
        digest = request.digest
        with self.session.transaction() as db:
            snapshot = self.snapshots.get(request.execution_id)
            old = AttemptRecord.one(
                db, execution_id=request.execution_id, owner_token_digest=digest
            )
            if old is not None:
                return AlreadyApplied(StartResult(snapshot, request.replayed_fence(snapshot, old)))
            if AttemptRecord.one(db, owner_token_digest=digest) is not None:
                raise IdentityConflict("prepared fence token has already been issued")
            request.validate(snapshot, self.participants.get(snapshot.execution.owner_lookup))
            self._activate(snapshot, request)
            self._resume_retry(snapshot, request)
            return Applied(StartResult(self.snapshots.get(request.execution_id), request.fence(1)))

    def _activate(self, snapshot: RecoverySnapshot, request: AttemptStart) -> None:
        """Publish attempt, execution and current pointer in the admitted transaction."""
        db, execution = self.session._connection, snapshot.execution
        created = self.session.now(execution.updated_at_ms)
        # Versioned fixed policy: no caller-selected initial lease, no semantic mirror.
        lease = created + INITIAL_LEASE_DURATION_MS
        AttemptRecord(
            execution_id=request.execution_id,
            attempt_ordinal=request.attempt_ordinal,
            owner_lookup=execution.owner_lookup,
            owner_thread=request.owner_thread,
            owner_generation=request.owner_generation,
            owner_token_digest=request.digest,
            lifecycle=PromptStartingAttempt(lease),
            revision=1,
            last_progress_at_ms=None,
            reason_code=None,
            created_at_ms=created,
            updated_at_ms=created,
        ).insert(db)
        ExecutionRecord.update(
            db,
            where="execution_id=?",
            parameters=(request.execution_id,),
            lifecycle=ActiveExecution(request.attempt_ordinal),
            revision=execution.revision + 1,
            reason_code=None,
            updated_at_ms=created,
        )
        CurrentExecutions.update(
            db,
            where="owner_lookup=?",
            parameters=(execution.owner_lookup,),
            execution_id=request.execution_id,
            attempt_ordinal=request.attempt_ordinal,
            pointer_revision=snapshot.pointer_revision + 1,
        )

    def _resume_retry(self, snapshot: RecoverySnapshot, request: AttemptStart) -> None:
        """Revoke prior replay proof and reengage coupled response/claim state atomically."""
        db, execution = self.session._connection, snapshot.execution
        if (
            request.attempt_ordinal > 1
            and snapshot.replay is not None
            and snapshot.replay.replay_safe
        ):
            # Replay safety is execution-scoped in v2, not bound to the new
            # attempt.  Monotonically revoke the previous attempt's proof;
            # a further automatic retry needs a separately versioned schema.
            updated = ReplayAssessments.update(
                db,
                where="execution_id=?",
                parameters=(request.execution_id,),
                replay_safe=False,
                revision=snapshot.replay.revision + 1,
            )
            if updated.rowcount != 1:
                raise IntegrityViolationError("retry safety revocation lost its assessment")
        if (
            execution.origin is ExecutionOrigin.WIRE
            and snapshot.obligation is not None
            and snapshot.obligation.lifecycle.deferred
        ):
            ResponseObligation.update(
                db,
                where="execution_id=?",
                parameters=(request.execution_id,),
                lifecycle=PendingResponse(),
                revision=snapshot.obligation.revision + 1,
                reason_code=None,
                updated_at_ms=self.session.now(snapshot.obligation.updated_at_ms),
            )
        if execution.lifecycle.retry:
            for assignment in snapshot.assignments:
                WakeAssignment.update(
                    db,
                    where="assignment_id=?",
                    parameters=(assignment.assignment_id,),
                    lifecycle=EngagedAssignment.build(
                        assignment.lifecycle.mode,
                        assignment.lifecycle.execution_id,
                        assignment.lifecycle.exact_target,
                    ),
                    revision=assignment.revision + 1,
                    updated_at_ms=self.session.now(assignment.updated_at_ms),
                )

    def renew_lease(
        self,
        fence: OwnerFence,
        *,
        expected_pointer_revision: int,
        duration_ms: int,
    ) -> Applied[StartResult]:
        if not 1 <= duration_ms <= MAX_LEASE_RENEWAL_MS:
            raise ValueError("renewal duration is outside the bounded policy")
        with self.session.transaction() as db:
            snapshot, attempt = self.require_fence(fence)
            if snapshot.pointer_revision != expected_pointer_revision:
                raise StaleRevision("pointer revision changed")
            if attempt.lifecycle.process_dead:
                raise RecoveryBlocked("a dead Pi RPC subprocess cannot renew its live lease")
            expiry = max(
                attempt.lifecycle.lease_expires_at_ms or 0,
                self.session.now(attempt.updated_at_ms) + duration_ms,
            )
            AttemptRecord.update(
                db,
                where="execution_id=? AND attempt_ordinal=?",
                parameters=(fence.execution_id, fence.attempt_ordinal),
                lifecycle=replace(attempt.lifecycle, lease_expires_at_ms=expiry),
                revision=attempt.revision + 1,
                updated_at_ms=self.session.now(attempt.updated_at_ms),
            )
            after = self.snapshots.get(fence.execution_id)
            assert after.attempt is not None
            return Applied(StartResult(after, replace(fence, revision=after.attempt.revision)))

    def advance(
        self,
        fence: OwnerFence,
        phase: type[AttemptState],
        *,
        expected_pointer_revision: int,
        backend_done: bool = False,
        process_dead: bool = False,
        progress: bool = False,
        reason_code: str | None = None,
    ) -> Applied[StartResult]:
        _bounded_reason(reason_code)
        if any(type(value) is not bool for value in (backend_done, process_dead, progress)):
            raise ValueError("attempt finality and progress must be booleans")
        if phase.terminal:
            raise IdentityConflict("terminal phases require atomic settlement")
        with self.session.transaction():
            snapshot, attempt = self.require_fence(fence)
            if snapshot.pointer_revision != expected_pointer_revision:
                raise StaleRevision("pointer revision changed")
            return Applied(
                self.advance_checked(
                    fence,
                    attempt,
                    phase,
                    backend_done=backend_done,
                    process_dead=process_dead,
                    progress=progress,
                    reason_code=reason_code,
                )
            )

    def advance_checked(
        self,
        fence: OwnerFence,
        attempt: AttemptRecord,
        phase: type[AttemptState],
        *,
        backend_done: bool,
        process_dead: bool,
        progress: bool,
        reason_code: str | None,
    ) -> StartResult:
        """Record a checked phase inside the caller's fenced transaction."""
        db = self.session._connection
        if phase is not type(attempt.lifecycle) and phase not in attempt.lifecycle.successors():
            raise IdentityConflict("attempt phase edge is not declared")
        if attempt.lifecycle.process_dead or attempt.lifecycle.backend_done:
            # Once either finality fact is recorded, the backend cannot
            # emit another phase or progress observation.  The other fact
            # may arrive later on the SAME phase before atomic settlement.
            new_final_fact = (backend_done and not attempt.lifecycle.backend_done) or (
                process_dead and not attempt.lifecycle.process_dead
            )
            if phase is not type(attempt.lifecycle) or progress or not new_final_fact:
                raise RecoveryBlocked("final backend evidence forbids further phase or progress")
        now = self.session.now(attempt.updated_at_ms)
        AttemptRecord.update(
            db,
            where="execution_id=? AND attempt_ordinal=?",
            parameters=(fence.execution_id, fence.attempt_ordinal),
            lifecycle=phase.load(
                attempt.lifecycle.lease_expires_at_ms,
                attempt.lifecycle.backend_done or backend_done,
                attempt.lifecycle.process_dead or process_dead,
            ),
            revision=attempt.revision + 1,
            updated_at_ms=now,
            last_progress_at_ms=now if progress else attempt.last_progress_at_ms,
            reason_code=reason_code,
        )
        after = self.snapshots.get(fence.execution_id)
        assert after.attempt is not None
        return StartResult(after, replace(fence, revision=after.attempt.revision))

    def observe_replay(
        self,
        fence: OwnerFence,
        *,
        expected_pointer_revision: int,
        expected_replay_revision: int | None,
        facts: ReplayFact,
        replay_safe: bool,
        side_effects_possible: bool,
    ) -> Applied[RecoverySnapshot] | AlreadyApplied[RecoverySnapshot]:
        if type(replay_safe) is not bool or type(side_effects_possible) is not bool:
            raise ValueError("replay assessment flags must be booleans")
        facts = ReplayFact(facts)
        with self.session.transaction():
            snapshot, _ = self.require_fence(fence)
            if snapshot.pointer_revision != expected_pointer_revision:
                raise StaleRevision("pointer revision changed")
            before = snapshot.replay
            if (before.revision if before else None) != expected_replay_revision:
                raise StaleRevision("replay assessment revision changed")
            after = ReplayAssessments(
                fence.execution_id,
                facts,
                replay_safe,
                side_effects_possible,
                1 if before is None else before.revision + 1,
            )
            return self.record_replay(snapshot, after)

    def record_replay(
        self,
        snapshot: RecoverySnapshot,
        after: ReplayAssessments,
    ) -> Applied[RecoverySnapshot] | AlreadyApplied[RecoverySnapshot]:
        """Persist monotonic replay evidence inside the checked transaction."""
        db = self.session._connection
        before = snapshot.replay
        if before is not None:
            if (before.facts, before.replay_safe, before.side_effects_possible) == (
                after.facts,
                after.replay_safe,
                after.side_effects_possible,
            ):
                return AlreadyApplied(snapshot)
            if (
                (before.facts | after.facts) != after.facts
                or (not before.replay_safe and after.replay_safe)
                or (before.side_effects_possible and not after.side_effects_possible)
            ):
                raise IdentityConflict("replay facts cannot be erased")
            ReplayAssessments.update(
                db,
                where="execution_id=?",
                parameters=(after.execution_id,),
                facts=after.facts,
                replay_safe=after.replay_safe,
                side_effects_possible=after.side_effects_possible,
                revision=before.revision + 1,
            )
        else:
            after.insert(db)
        return Applied(self.snapshots.get(after.execution_id))

    def fail_unknown(
        self,
        fence: OwnerFence,
        *,
        expected_pointer_revision: int,
    ) -> Applied[RecoverySnapshot]:
        """Atomically fail a fenced, reaped backend; UNKNOWN is never replayable.

        The live runner holds registry owner exclusion and has completed native
        child/tool cleanup. Finality here is local, not provider acceptance.
        """
        with self.session.transaction():
            snapshot, attempt = self.require_fence(fence)
            if snapshot.pointer_revision != expected_pointer_revision:
                raise StaleRevision("pointer revision changed")
            if snapshot.publication_intent is not None:
                raise PublicationUncertain("UNKNOWN failure cannot resolve frozen publication")
            before = snapshot.replay
            self.record_replay(
                snapshot,
                ReplayAssessments(
                    fence.execution_id,
                    (before.facts if before is not None else ReplayFact.NONE)
                    | ReplayFact.UNKNOWN_EFFECTS,
                    False,
                    True,
                    1 if before is None else before.revision + 1,
                ),
            )
            if not (attempt.lifecycle.backend_done and attempt.lifecycle.process_dead):
                self.advance_checked(
                    fence,
                    attempt,
                    type(attempt.lifecycle),
                    backend_done=True,
                    process_dead=True,
                    progress=False,
                    reason_code="native_unknown",
                )
            return Applied(
                self.settle_checked(
                    self.snapshots.get(fence.execution_id),
                    success=False,
                    reason_code="native_unknown",
                )
            )

    def observe_connectivity(
        self,
        fence: OwnerFence,
        *,
        expected_pointer_revision: int,
        expected_revision: int | None,
        owner: OwnerConnectivity,
        acp_client: ACPClientConnectivity,
    ) -> Applied[RecoverySnapshot]:
        owner, acp_client = OwnerConnectivity(owner), ACPClientConnectivity(acp_client)
        with self.session.transaction() as db:
            snapshot, _ = self.require_fence(fence)
            before = snapshot.connectivity
            if (
                snapshot.pointer_revision != expected_pointer_revision
                or (before.revision if before else None) != expected_revision
            ):
                raise StaleRevision("connectivity or pointer revision changed")
            now = self.session.now(before.observed_at_ms if before else 0)
            ConnectivityFacet(
                fence.execution_id,
                owner,
                acp_client,
                before.revision + 1 if before else 1,
                now,
            ).upsert(db)
            return Applied(self.snapshots.get(fence.execution_id))

    def append_recovery_audit(
        self,
        fence: OwnerFence,
        *,
        expected_pointer_revision: int,
        kind: type[RecoveryCondition],
        reason_code: str,
        sanitized_detail: str | None = None,
        elapsed_ms: int = 0,
    ) -> Applied[RecoverySnapshot]:
        _bounded_reason(reason_code)
        if (
            reason_code is None
            or elapsed_ms < 0
            or (sanitized_detail is not None and len(sanitized_detail) > MAX_SANITIZED_DETAIL_CHARS)
        ):
            raise ValueError("invalid recovery audit")
        with self.session.transaction() as db:
            snapshot, attempt = self.require_fence(fence)
            if snapshot.pointer_revision != expected_pointer_revision:
                raise StaleRevision("pointer revision changed")
            kind.validate_audit(snapshot, attempt)
            RecoveryAudit(
                execution_id=fence.execution_id,
                kind=kind,
                reason_code=reason_code,
                sanitized_detail=sanitized_detail,
                attempt=attempt.attempt_ordinal,
                elapsed_ms=elapsed_ms,
                observed_at_ms=self.session.now(attempt.updated_at_ms),
            ).insert(db)
            return Applied(self.snapshots.get(fence.execution_id))

    def settle_checked(
        self,
        snapshot: RecoverySnapshot,
        *,
        success: bool,
        reason_code: str | None,
    ) -> RecoverySnapshot:
        db = self.session._connection
        execution, attempt = snapshot.execution, snapshot.attempt
        if snapshot.publication_intent is not None:
            raise PublicationUncertain("publication requires bus-keyed receipt resolution")
        if (
            attempt is None
            or not snapshot.is_current
            or not (attempt.lifecycle.backend_done and attempt.lifecycle.process_dead)
        ):
            raise RecoveryBlocked("settlement requires exact final done/death evidence")
        if success:
            if not attempt.lifecycle.settling:
                raise IdentityConflict("silent completion requires settling phase")
            if snapshot.obligation is not None and not snapshot.obligation.lifecycle.retryable:
                raise IdentityConflict("wire completion requires nonpublication obligation")
        else:
            authorized = retry_disposition_authorized(
                execution, snapshot.replay, snapshot.obligation
            )
        now = self.session.now(max(execution.updated_at_ms, attempt.updated_at_ms))
        AttemptRecord.update(
            db,
            where="execution_id=? AND attempt_ordinal=?",
            parameters=(execution.execution_id, attempt.attempt_ordinal),
            lifecycle=SucceededAttempt() if success else AttemptFailedAttempt(),
            revision=attempt.revision + 1,
            updated_at_ms=now,
            reason_code=reason_code,
        )
        execution_state = (
            CompletedExecution(attempt.attempt_ordinal)
            if success
            else (
                DeferredExecution(attempt.attempt_ordinal)
                if authorized
                else FailedExecution(attempt.attempt_ordinal)
            )
        )
        ExecutionRecord.update(
            db,
            where="execution_id=?",
            parameters=(execution.execution_id,),
            lifecycle=execution_state,
            revision=execution.revision + 1,
            updated_at_ms=now,
            reason_code=reason_code,
        )
        if snapshot.obligation is not None:
            obligation = snapshot.obligation
            response = (
                SilentResponse()
                if success
                else (DeferredResponse() if authorized else FailedResponse())
            )
            ResponseObligation.update(
                db,
                where="execution_id=?",
                parameters=(execution.execution_id,),
                lifecycle=response,
                revision=obligation.revision + 1,
                updated_at_ms=self.session.now(obligation.updated_at_ms),
                reason_code=reason_code,
            )
        for assignment in snapshot.assignments:
            WakeAssignment.update(
                db,
                where="assignment_id=?",
                parameters=(assignment.assignment_id,),
                lifecycle=execution_state.assignment_state().build(
                    assignment.lifecycle.mode,
                    assignment.lifecycle.execution_id,
                    assignment.lifecycle.exact_target,
                ),
                revision=assignment.revision + 1,
                updated_at_ms=self.session.now(assignment.updated_at_ms),
            )
        CurrentExecutions.update(
            db,
            where="owner_lookup=?",
            parameters=(execution.owner_lookup,),
            execution_id=None,
            attempt_ordinal=None,
            pointer_revision=snapshot.pointer_revision + 1,
        )
        return self.snapshots.get(execution.execution_id)

    def settle_nonpublication(
        self,
        fence: OwnerFence,
        *,
        expected_pointer_revision: int,
        success: bool,
        reason_code: str | None = None,
    ) -> Applied[RecoverySnapshot]:
        if type(success) is not bool:
            raise ValueError("settlement success must be a boolean")
        _bounded_reason(reason_code)
        with self.session.transaction():
            snapshot, _ = self.require_fence(fence)
            if snapshot.pointer_revision != expected_pointer_revision:
                raise StaleRevision("pointer revision changed")
            return Applied(self.settle_checked(snapshot, success=success, reason_code=reason_code))
