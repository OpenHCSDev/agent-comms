"""Live goal-turn capabilities; durable reservations stay in the attempt ledger."""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import ExitStack
from typing import TYPE_CHECKING, ClassVar

from agent_comms.ordinary_turn_rules import OrdinaryGoalGrantCheck

if TYPE_CHECKING:
    from agent_comms.goal_attempts import GoalAttemptStore, LaunchPermit
    from agent_comms.goal_management import Goals
    from agent_comms.goals import GoalDeclaration
    from agent_comms.pi_payloads import PiUsage
    from agent_comms.thread_identity import RegisteredTurn
    from agent_comms.threads import Thread
    from agent_comms.turn_context import TurnContext
    from agent_comms.turn_goal_account import TurnGoalAccount, VerifiedGoalSettlement
    from agent_comms.turn_terminal import TurnTerminal


class GoalTurnPermit(ABC):
    """The turn's launch authority, including a turn that has no goal claim."""

    @property
    @abstractmethod
    def claims(self) -> tuple[LaunchPermit, ...]: ...

    def check_grant(self, store: GoalAttemptStore) -> None:
        for permit in self.claims:
            OrdinaryGoalGrantCheck(permit=permit, store=store).require_valid()

    def prepare_launch(self, claims: ExitStack, store: GoalAttemptStore) -> bool:
        for permit in self.claims:
            claims.callback(permit.retire_unverified, store)
        return bool(self.claims)

    def settle(self, account: TurnGoalAccount, terminal: TurnTerminal) -> None:
        for permit in self.claims:
            account.settle_launched(permit, terminal)

    def stop(self, store: GoalAttemptStore) -> None:
        for permit in self.claims:
            permit.stop(store)

    def settle_goal(self, settlement: type[VerifiedGoalSettlement], store: GoalAttemptStore,
                    goal: GoalDeclaration, turn: RegisteredTurn) -> None:
        for permit in self.claims:
            goal.settle(settlement, store, permit, turn)

    @abstractmethod
    def has_verified_progress(self, store: GoalAttemptStore) -> bool: ...

    @abstractmethod
    def fail_origin(self, store: GoalAttemptStore, goal_id: str) -> None: ...

    @abstractmethod
    def charge_for(self, goal_id: str, goals: Goals, response: str,
                   usage: PiUsage) -> bool: ...

    @abstractmethod
    def add_context(self, context: TurnContext, owner: Thread, goals: Goals) -> TurnContext: ...


class NoLaunchPermit(GoalTurnPermit):
    claims: ClassVar[tuple[LaunchPermit, ...]] = ()

    def has_verified_progress(self, store: GoalAttemptStore) -> bool:
        return False

    def charge_for(self, goal_id: str, goals: Goals, response: str,
                   usage: PiUsage) -> bool:
        return False

    def add_context(self, context: TurnContext, owner: Thread, goals: Goals) -> TurnContext:
        return context

    def fail_origin(self, store: GoalAttemptStore, goal_id: str) -> None:
        generation = store.snapshot(goal_id)
        if generation and generation.lifecycle.allows_resume(True):
            store.retire_goal(goal_id, expected_generation=generation.number,
                              attempt_id=generation.attempt_id)
