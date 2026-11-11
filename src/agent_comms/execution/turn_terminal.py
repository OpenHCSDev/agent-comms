from __future__ import annotations

import asyncio
from abc import (
    ABC,
    abstractmethod,
)
from dataclasses import (
    dataclass,
    field,
)
from typing import TYPE_CHECKING

from agent_comms.execution.turn_failure import BackendFailed, NoTurnFailure, TurnFailure

if TYPE_CHECKING:
    from agent_comms.agent_events import Done
    from agent_comms.execution.owned_turn import TurnGoalAccount
    from agent_comms.execution.turn_progress import TurnProgress
    from agent_comms.storage.maintenance_barrier import MaintenanceBarrier


class TurnInterruption(ABC):
    @abstractmethod
    def interrupting(self, prior: TurnTerminal) -> TurnTerminal: ...


class TurnTerminal(TurnInterruption):
    """Own interruption and settlement policy, never a nullable result flag."""

    def interrupting(self, prior: TurnTerminal) -> TurnTerminal:
        return prior

    def reported(self, event: Done) -> Done:
        return event

    def require_reported(self) -> Done:
        raise RuntimeError("Turn has no reported backend result")

    async def prepare_publication(self, progress: TurnProgress) -> Done:
        return await progress.record_failure(
            EndedWithoutResult(failure=progress.terminal_failure).turn_failure)

    async def report_failure(self, progress: TurnProgress, error: Exception, kind: type[TurnFailure]):
        cause = error.__cause__ or error
        detail = f"Native turn failed: {type(cause).__name__}: {cause}"
        return (await progress.record_failure(kind(detail))).outcome

    def settle_current(self, account: TurnGoalAccount) -> None:
        pass

    @abstractmethod
    def settle_launched(self, account, permit, store, captured, current) -> None: ...

    def settle_origin(self, account, permit, store, goal_id, current) -> None:
        if not permit.has_verified_progress(store):
            permit.fail_origin(store, goal_id)


class FailedGoalTurn(TurnTerminal):
    """Reported failures and missing results share the original failed ledger path.

    Both paths end the turn with the failure it actually ended with, so this is where
    that failure lives and where its one fallback is declared. Done already carried it;
    the no-result path restated the fallback alone and dropped the failure.
    """

    text: str
    failure: TurnFailure = NoTurnFailure()

    def failure_observation(self, account, permit, captured):
        snapshot = account.comms.registry.snapshot()
        return permit.failure_observation(
            owner=account.owner,
            goal=captured,
            lease=account.lease,
            turn_id=account.turn,
            claim_scope=account.lease.claimed_generation,
            current_owner=snapshot.threads.get(account.thread_name),
            current_claim=snapshot.claim_generation(account.thread_name),
            failure=self.turn_failure,
        )

    @property
    def turn_failure(self) -> TurnFailure:
        """The failure this turn ended with, its text included: the ledger, the diagnostic and
        the blocked line all say what broke, never only which kind."""
        return self.failure.declared_or(BackendFailed(self.text))

    def settle_launched(self, account, permit, store, captured, current) -> None:
        if permit.has_verified_progress(store):
            return
        self.turn_failure.settle_attempt(
            permit, store, self.failure_observation(account, permit, captured))


@dataclass(frozen=True)
class EndedWithoutResult(FailedGoalTurn):
    """No native terminal reported, so the turn keeps whatever failure it did end with.

    The recorded failure arrives already declared by TurnProgress; the generic text
    below is the fallback for a turn that recorded no evidence at all.
    """

    text: str = "Native turn ended without a result"
    failure: TurnFailure = field(default_factory=NoTurnFailure)


class OperatorStopped(TurnTerminal):
    def interrupting(self, prior: TurnTerminal) -> TurnTerminal:
        return self

    def settle_launched(self, account, permit, store, captured, current) -> None:
        self.settle_origin(account, permit, store, captured.id, current)

    def settle_origin(self, account, permit, store, goal_id, current) -> None:
        if any(current.matching(goal_id).active()):
            permit.stop(store)


class OwnerStop(asyncio.Future[TurnTerminal], TurnInterruption):
    """The worker's existing stop wait carries the operator's actual reason.

    An install holds maintenance before it signals owners, so a turn that ends in
    that window reads the hold, not a backend failure the signal has not yet named.
    """

    def __init__(self, maintenance: MaintenanceBarrier) -> None:
        super().__init__()
        self.maintenance = maintenance

    def interrupting(self, prior: TurnTerminal) -> TurnTerminal:
        return self.terminal.interrupting(prior)

    def request(self) -> None:
        if not self.done():
            self.set_result(OperatorStopped())

    @property
    def terminal(self) -> TurnTerminal:
        if self.done():
            return self.result()
        # A held ingress is the install's stop, declared before any owner is signalled.
        return self.maintenance.read().selected(
            held=OperatorStopped(), granted=EndedWithoutResult())
