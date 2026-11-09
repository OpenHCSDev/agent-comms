from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from metaclass_registry.mro_dispatch import (
    MroDispatch,
    handles,
)

from agent_comms.execution.reported_turn import ReportedTurn
from agent_comms.nominal.field_codec import TurnId
from agent_comms.planning.attempt_resolution import VerifiedTurnDecision
from agent_comms.planning.goal_attempts import GoalAttemptStore, LaunchPermit
from agent_comms.planning.goal_states import ActiveGoal, CompletedGoal, PausedGoal
from agent_comms.planning.goals import Goal

if TYPE_CHECKING:
    from agent_comms.comms import Comms
    from agent_comms.registry.threads import Thread


@dataclass
class VerifiedGoalSettlement(MroDispatch):
    store: GoalAttemptStore
    permit: LaunchPermit
    goal: Goal
    turn: TurnId

    @classmethod
    def settle_current(cls, comms: Comms, owner: Thread, turn: TurnId) -> None:
        """Every verified owner turn, Toad-typed or bus-selected, continues its current goal."""
        with comms.registry.store.current_snapshot() as cut:
            current = cut.snapshot.require(cut.snapshot.canonical_name(owner.name))
            if current.incarnation != owner.incarnation or current.worktree != owner.worktree:
                return
            for goal in owner.goal.continued_by(current.goal):
                store = comms.goals.open_goal_store()
                generation = store.require_generation(goal.id)
                generation.lifecycle.continue_work(
                    store, generation, VerifiedTurnDecision(turn)
                )

    @property
    def progress_witness(self) -> str:
        return f"native-terminal:{self.turn}"

    @property
    def completion_witness(self) -> str:
        return f"registry-revision:{self.goal.revision}"

    @handles(ActiveGoal, PausedGoal)
    def progress(self, state) -> None:
        # An owner pause prevents another launch, not recording verified work.
        self.permit.record_verified_progress(self.store, self.progress_witness)

    @handles(CompletedGoal)
    def completion(self, state: CompletedGoal) -> None:
        if self.goal.reports_turn(ReportedTurn(self.turn)):
            self.permit.record_verified_completion(self.store, self.completion_witness)


class OriginGoalSettlement(VerifiedGoalSettlement):
    @property
    def progress_witness(self) -> str:
        return f"origin-final:{self.turn}"

    @property
    def completion_witness(self) -> str:
        return f"origin-completed:{self.goal.revision}"
