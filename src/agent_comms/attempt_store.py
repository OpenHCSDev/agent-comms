"""Attempt store: owned coordinator state and transitions."""

from __future__ import annotations

from dataclasses import dataclass, replace

from agent_comms.reason_codes import GivenReasonCode, NoDetail, NoReasonCode, ReasonCode, RecoveryDetail
from agent_comms.assignment_states import EngagedAssignment
from agent_comms.attempt_start import AttemptStart
from agent_comms.attempt_states import AttemptState, TerminalAttempt, UnknownOutcomeAttempt
from agent_comms.coordination_errors import (
    IdentityConflict,
    PublicationUncertain,
    StaleFence,
    StaleRevision,
)
from agent_comms.coordination_results import AlreadyApplied, Applied
from agent_comms.coordination_session import NativeBoundary
from agent_comms.coordination_snapshot import RecoverySnapshot
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.coordination_tables.attempts import AttemptRecord, ReplayFact
from agent_comms.coordination_tables.recovery import RecoveryAudit
from agent_comms.owner_fence import OwnerFence
from agent_comms.turn_failure import TurnFailure
from agent_comms.recovery_reader import RecoveryReader
from agent_comms.recovery_states import RecoveryCondition


@dataclass(frozen=True, slots=True)
class StartResult:
    snapshot: RecoverySnapshot
    fence: OwnerFence


MAX_LEASE_RENEWAL_MS = 300_000


class AttemptStore(RecoveryReader):
    def require_fence(self, fence: OwnerFence) -> tuple[RecoverySnapshot, AttemptRecord]:
        snapshot = self.get(fence.execution_id)
        attempt = snapshot.require_current_attempt()
        if (
            attempt.authority != fence.authority
            or self.participants.get(attempt.owner_lookup).owner_identity.generation
            != attempt.owner_generation
        ):
            raise StaleFence("attempt fence is not current")
        if attempt.revision != fence.revision:
            raise StaleRevision("attempt fence revision changed")
        return snapshot, attempt

    def start(self, request: AttemptStart) -> Applied[StartResult] | AlreadyApplied[StartResult]:
        digest = request.digest
        with self.session.writing() as db:
            snapshot = self.get(request.execution_id)
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
            return Applied(StartResult(self.get(request.execution_id), request.fence(1)))

    def _resume_retry(self, snapshot: RecoverySnapshot, request: AttemptStart) -> None:
        """Revoke prior replay proof and reengage coupled response/claim state atomically."""
        db, execution = self.session._connection, snapshot.execution
        for obligation in snapshot.obligations:
            obligation.lifecycle.resume_response(self.session, obligation, request.execution_id)
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
        with self.session.writing() as db:
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
            after = self.get(fence.execution_id)
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
        reason_code: ReasonCode = NoReasonCode(),
    ) -> Applied[StartResult]:
        if any(type(value) is not bool for value in (backend_done, process_dead, progress)):
            raise ValueError("attempt finality and progress must be booleans")
        if phase.items_with(TerminalAttempt, phase):
            raise IdentityConflict("terminal phases require atomic settlement")
        with self.session.writing():
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
            after = self.get(fence.execution_id)
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
        with self.session.writing():
            snapshot, _ = self.require_fence(fence)
            if snapshot.pointer_revision != expected_pointer_revision:
                raise StaleRevision("pointer revision changed")
            before = snapshot.replay
            before.require_revision(expected_replay_revision)
            if not before.observe(
                self.session._connection, fence.execution_id, facts=facts,
                replay_safe=replay_safe, side_effects_possible=side_effects_possible,
            ):
                return AlreadyApplied(snapshot)
            return Applied(self.get(fence.execution_id))

    def fail_unknown(
        self,
        fence: OwnerFence,
        *,
        expected_pointer_revision: int,
        boundary: NativeBoundary | None = None,
    ) -> Applied[RecoverySnapshot]:
        """Atomically fail a fenced, reaped backend; UNKNOWN is never replayable.

        The live runner holds registry owner exclusion and has completed native
        child/tool cleanup. Finality here is local, not provider acceptance.
        """
        with self.session.settlement(boundary) as acquired:
            snapshot, attempt = self.require_fence(fence)
            if snapshot.pointer_revision != expected_pointer_revision:
                raise StaleRevision("pointer revision changed")
            if bool(snapshot.publication_intents):
                raise PublicationUncertain("UNKNOWN failure cannot resolve frozen publication")
            if not (attempt.lifecycle.backend_done and attempt.lifecycle.process_dead):
                attempt.advance(
                    self.session,
                    type(attempt.lifecycle),
                    backend_done=True,
                    process_dead=True,
                    progress=False,
                    reason_code=GivenReasonCode("native_unknown"),
                )
            self.get(fence.execution_id).settle(
                self.session, UnknownOutcomeAttempt(), GivenReasonCode("native_unknown"), acquired
            )
            return Applied(self.get(fence.execution_id))

    def append_recovery_audit(
        self,
        fence: OwnerFence,
        *,
        expected_pointer_revision: int,
        kind: type[RecoveryCondition],
        reason_code: GivenReasonCode,
        sanitized_detail: RecoveryDetail = NoDetail(),
        elapsed_ms: int = 0,
    ) -> Applied[RecoverySnapshot]:
        if elapsed_ms < 0:
            raise ValueError("invalid recovery audit")
        with self.session.writing() as db:
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
            return Applied(self.get(fence.execution_id))

    def settle_nonpublication(
        self,
        fence: OwnerFence,
        *,
        expected_pointer_revision: int,
        outcome: TerminalAttempt,
        reason_code: ReasonCode = NoReasonCode(),
        failure: TurnFailure | None = None,
    ) -> Applied[RecoverySnapshot]:
        if not isinstance(outcome, TerminalAttempt):
            raise ValueError("settlement requires a terminal attempt outcome")
        with self.session.settlement() as acquired:
            snapshot, _ = self.require_fence(fence)
            if snapshot.pointer_revision != expected_pointer_revision:
                raise StaleRevision("pointer revision changed")
            if failure is not None:
                failure.prepare_execution_retry(
                    self.session._connection, fence.execution_id
                )
                snapshot, _ = self.require_fence(fence)
            snapshot.settle(self.session, outcome, reason_code, acquired)
            return Applied(self.get(fence.execution_id))
