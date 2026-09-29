"""Attempt store: owned coordinator state and transitions."""

from __future__ import annotations

from dataclasses import dataclass, replace

from agent_comms.assignment_states import EngagedAssignment
from agent_comms.attempt_start import AttemptStart
from agent_comms.attempt_states import AttemptFailedAttempt, AttemptState, TerminalAttempt
from agent_comms.coordination_contracts import (
    MAX_SANITIZED_DETAIL_CHARS,
    _bounded_reason,
)
from agent_comms.coordination_errors import (
    IdentityConflict,
    IntegrityViolationError,
    PublicationUncertain,
    StaleFence,
    StaleRevision,
)
from agent_comms.coordination_results import AlreadyApplied, Applied
from agent_comms.coordination_session import CoordinationSession
from agent_comms.coordination_snapshot import RecoverySnapshot
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.coordination_tables.attempts import AttemptRecord, ReplayAssessments, ReplayFact
from agent_comms.coordination_tables.executions import ExecutionOrigin
from agent_comms.coordination_tables.recovery import (
    ACPClientConnectivity,
    ConnectivityFacet,
    OwnerConnectivity,
    RecoveryAudit,
)
from agent_comms.coordination_tables.responses import ResponseObligation
from agent_comms.obligation_states import PendingResponse
from agent_comms.owner_fence import OwnerFence
from agent_comms.participant_store import ParticipantStore
from agent_comms.recovery_reader import RecoveryReader
from agent_comms.recovery_states import RecoveryCondition


@dataclass(frozen=True, slots=True)
class StartResult:
    snapshot: RecoverySnapshot
    fence: OwnerFence


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
            request.activate(self.session, snapshot)
            self._resume_retry(snapshot, request)
            return Applied(StartResult(self.snapshots.get(request.execution_id), request.fence(1)))

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
            lifecycle = attempt.lifecycle.renewed(
                self.session.now(attempt.updated_at_ms) + duration_ms
            )
            AttemptRecord.update(
                db,
                where="execution_id=? AND attempt_ordinal=?",
                parameters=(fence.execution_id, fence.attempt_ordinal),
                lifecycle=lifecycle,
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
            attempt.advance(
                self.session,
                phase,
                backend_done=backend_done,
                process_dead=process_dead,
                progress=progress,
                reason_code=reason_code,
            )
            after = self.snapshots.get(fence.execution_id)
            return Applied(
                StartResult(after, replace(fence, revision=after.require_attempt().revision))
            )

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
            if not after.record(self.session._connection, snapshot.replay):
                return AlreadyApplied(snapshot)
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
            ReplayAssessments(
                fence.execution_id,
                (before.facts if before is not None else ReplayFact.NONE)
                | ReplayFact.UNKNOWN_EFFECTS,
                False,
                True,
                1 if before is None else before.revision + 1,
            ).record(self.session._connection, before)
            if not (attempt.lifecycle.backend_done and attempt.lifecycle.process_dead):
                attempt.advance(
                    self.session,
                    type(attempt.lifecycle),
                    backend_done=True,
                    process_dead=True,
                    progress=False,
                    reason_code="native_unknown",
                )
            self.snapshots.get(fence.execution_id).settle(
                self.session, AttemptFailedAttempt(), "native_unknown"
            )
            return Applied(self.snapshots.get(fence.execution_id))

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

    def settle_nonpublication(
        self,
        fence: OwnerFence,
        *,
        expected_pointer_revision: int,
        outcome: TerminalAttempt,
        reason_code: str | None = None,
    ) -> Applied[RecoverySnapshot]:
        if not isinstance(outcome, TerminalAttempt):
            raise ValueError("settlement requires a terminal attempt outcome")
        _bounded_reason(reason_code)
        with self.session.transaction():
            snapshot, _ = self.require_fence(fence)
            if snapshot.pointer_revision != expected_pointer_revision:
                raise StaleRevision("pointer revision changed")
            snapshot.settle(self.session, outcome, reason_code)
            return Applied(self.snapshots.get(fence.execution_id))
