from __future__ import annotations

from abc import (
    ABC,
    abstractmethod,
)
from contextlib import ExitStack
from typing import (
    ClassVar,
    TYPE_CHECKING,
)

from ..nominal.field_codec import (
    GoalId,
    TurnId,
)
from ..turn_item import UsageChargeId

if TYPE_CHECKING:
    from .owned_turn import TurnGoalAccount
    from ..pi_wire import PiUsage
    from ..planning.goal_attempts import (
        GoalAttemptStore,
        LaunchPermit,
    )
    from ..planning.goal_management import Goals
    from ..planning.goals import GoalDeclaration
    from ..registry.threads import Thread
    from ..turn_context import TurnContext
    from ..turn_goal_account import VerifiedGoalSettlement
    from ..turn_terminal import TurnTerminal


class GoalTurnPermit(ABC):
    """The turn's launch authority, including a turn that has no goal claim."""

    @property
    @abstractmethod
    def claims(self) -> tuple[LaunchPermit, ...]: ...

    def check_grant(self, store: GoalAttemptStore) -> None:
        for permit in self.claims:
            permit.check_grant(store)

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
                    goal: GoalDeclaration, turn: TurnId) -> None:
        for permit in self.claims:
            goal.settle(settlement, store, permit, turn)

    @abstractmethod
    def has_verified_progress(self, store: GoalAttemptStore) -> bool: ...

    @abstractmethod
    def fail_origin(self, store: GoalAttemptStore, goal_id: GoalId) -> None: ...

    @abstractmethod
    def charge_for(self, goal_id: GoalId, goals: Goals, response: UsageChargeId,
                   usage: PiUsage) -> bool: ...

    @abstractmethod
    def add_context(self, context: TurnContext, owner: Thread, goals: Goals) -> TurnContext: ...


class NoLaunchPermit(GoalTurnPermit):
    claims: ClassVar[tuple[LaunchPermit, ...]] = ()

    def has_verified_progress(self, store: GoalAttemptStore) -> bool:
        return False

    def charge_for(self, goal_id: GoalId, goals: Goals, response: UsageChargeId,
                   usage: PiUsage) -> bool:
        return False

    def add_context(self, context: TurnContext, owner: Thread, goals: Goals) -> TurnContext:
        return context

    def fail_origin(self, store: GoalAttemptStore, goal_id: GoalId) -> None:
        generation = store.snapshot(goal_id)
        if generation and generation.lifecycle.allows_resume(True):
            store.retire_goal(goal_id, expected_generation=generation.number,
                              attempt_id=generation.attempt_id)
