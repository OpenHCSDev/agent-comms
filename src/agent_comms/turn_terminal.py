"""A turn always ends with a declared settlement, including an operator stop."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from contextlib import suppress
from typing import TYPE_CHECKING

from .diagnostics import FailureReason, terminal_failure_reason
from .goal_failure_observation import FailedTurnObservation
from .goal_attempts import StaleAttemptError
from .turn_goal_account import FailedGoalOrigin

if TYPE_CHECKING:
    from .agent_events import Done
    from .goal_attempts import LaunchPermit
    from .turn_goal_account import TurnGoalAccount
    from .turn_progress import TurnProgress


class TurnTerminal(ABC):
    """Own interruption and settlement policy, never a nullable result flag."""

    @staticmethod
    def from_cancellation(error: asyncio.CancelledError) -> TurnTerminal:
        # asyncio's cancellation-message boundary: our stop paths send the
        # terminal itself; a bare cancellation has no reported backend result.
        (terminal,) = error.args or (EndedWithoutResult(),)
        if not isinstance(terminal, TurnTerminal):
            raise TypeError("Turn cancellation requires a declared terminal")
        return terminal

    def interrupted(self, terminal: TurnTerminal) -> TurnTerminal:
        return self

    def observe_goal(self, account: TurnGoalAccount) -> None:
        pass

    def reported(self, event: Done) -> Done:
        return event

    def require_reported(self) -> Done:
        raise RuntimeError("Turn has no reported backend result")

    async def prepare_publication(self, progress: TurnProgress) -> Done:
        return await progress.record_failure("Native turn ended without a result")

    async def report_failure(self, progress: TurnProgress, error: Exception):
        cause = error.__cause__ or error
        detail = f"Native turn failed: {type(cause).__name__}: {cause}"
        return (await progress.record_failure(detail)).outcome

    def settle_current(self, account: TurnGoalAccount) -> None:
        pass

    @abstractmethod
    def settle_launched(self, account, permit, store, captured, current) -> None: ...

    def settle_origin(self, account, permit, store, goal_id, current) -> None:
        if not permit.has_verified_progress(store):
            permit.fail_origin(store, goal_id)
            for goal in current:
                FailedGoalOrigin(account.comms, account.thread_name, goal).dispatch_sync(goal.state)


class FailedGoalTurn(TurnTerminal):
    """Reported failures and missing results share the original failed ledger path."""

    def failure_observation(self, account, permit, captured):
        snapshot = account.comms.registry.snapshot()
        return FailedTurnObservation.from_terminal(
            permit.reservation,
            owner=account.owner,
            goal=captured,
            lease=account.lease,
            turn_id=account.turn.value,
            claim_scope=account.lease.claimed_generation,
            current_owner=snapshot.threads.get(account.thread_name),
            current_claim=snapshot.claim_generation(account.thread_name),
            reason=self.failure_reason,
        )

    @property
    @abstractmethod
    def failure_reason(self) -> FailureReason: ...

    def settle_launched(self, account, permit, store, captured, current) -> None:
        if permit.has_verified_progress(store):
            return
        observation = self.failure_observation(account, permit, captured)
        reason = self.failure_reason.value
        with suppress(StaleAttemptError):
            permit.reservation.fail(
                store, reason,
                observation=observation,
            )
        current.block_after_failed_turn(
            account.comms.goals, account.thread_name, account.owner.worktree,
            reason, turn=account.turn,
        )


class EndedWithoutResult(FailedGoalTurn):
    @property
    def failure_reason(self) -> FailureReason:
        return terminal_failure_reason({})

    def interrupted(self, terminal: TurnTerminal) -> TurnTerminal:
        return terminal


class OperatorStopped(TurnTerminal):
    def settle_launched(self, account, permit, store, captured, current) -> None:
        self.settle_origin(account, permit, store, captured.id, current)

    def settle_origin(self, account, permit, store, goal_id, current) -> None:
        if any(current.matching(goal_id).active()):
            permit.stop(store)

    async def prepare_publication(self, progress: TurnProgress) -> Done:
        raise asyncio.CancelledError(self)


class OwnerStop(asyncio.Future[TurnTerminal]):
    """The worker's existing stop wait carries the operator's actual reason."""

    def request(self) -> None:
        if not self.done():
            self.set_result(OperatorStopped())

    @property
    def terminal(self) -> TurnTerminal:
        return self.result() if self.done() else EndedWithoutResult()
