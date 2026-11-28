"""Read-only goal snapshot projection over the goal ledger and wait stores.

GoalRecords and Goals inherit this; lightweight CLI readers construct it directly
without importing goal actions, orders, or todo command machinery.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import cached_property

from agent_comms.execution.turn_lease import ActiveTurn, StoredTurnRecord, TurnDeclaration, TurnRecord
from agent_comms.nominal.field_codec import GoalId
from agent_comms.planning.goal_failure_observation import GoalFailureEvidence
from agent_comms.planning.goal_ledger import FailedTurnEvidence, Generation
from agent_comms.planning.goal_mutation import (
    GoalFailureProjection,
    GoalGenerationProjection,
    GoalStateProjection,
    GoalTurnProjection,
    GoalWaitProjection,
)
from agent_comms.planning.goal_presentation import (
    AttemptFailedExecution,
    AttemptUnresolvedExecution,
    CompletedExecution,
    GoalExecutionControls,
    GoalSnapshot,
    IdleGoalExecution,
    PausedExecution,
    RetryableBlockExecution,
    RunnableExecution,
    RunningExecution,
    StandbyExecution,
)
from agent_comms.planning.goal_snapshots import GoalSnapshots
from agent_comms.planning.goal_states import PauseSource
from agent_comms.planning.goal_store_access import GoalStoreAccess
from agent_comms.planning.goal_wait_target import GoalWaitTarget
from agent_comms.planning.goal_waits import GoalWait, OwnerWaitRead
from agent_comms.planning.goals import Goal, GoalDeclaration
from agent_comms.registry.agent_activity import ActivityAnswers
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.registry.threads import Thread


@dataclass(frozen=True, kw_only=True)
class GoalExecutionSource(
    OwnerWaitRead,
    GoalStateProjection[GoalExecutionControls, PauseSource],
    GoalFailureProjection[GoalExecutionControls, FailedTurnEvidence],
    GoalGenerationProjection[GoalExecutionControls],
    GoalWaitProjection[GoalExecutionControls],
    GoalTurnProjection[GoalExecutionControls, ActiveTurn],
):
    """One read's original owners; never persisted or used as launch authority."""

    goal: Goal
    generation: Generation
    evidence: GoalFailureEvidence[FailedTurnEvidence]
    agents: ActivityAnswers
    turns: Mapping[StoredTurnRecord, TurnDeclaration]

    @cached_property
    def turn(self) -> TurnDeclaration:
        return self.owner.turn_record.read_in(self.turns)

    @cached_property
    def owner_alive(self) -> bool:
        return self.owner.process_alive

    def active(self) -> GoalExecutionControls:
        return self.wait.execution(self)

    def generation_execution(self) -> GoalExecutionControls:
        return self.generation.lifecycle.execution(self)

    def idle_execution(self) -> GoalExecutionControls:
        return self.generation.lifecycle.idle_execution(self)

    def active_turn_execution(self, turn: ActiveTurn) -> GoalExecutionControls:
        if self.owner_alive and turn.owner == self.owner.process_identity:
            return RunningExecution(self.goal.id)
        return self.idle_execution()

    def standby_execution(self) -> GoalExecutionControls:
        targets = tuple(
            GoalWaitTarget(target.incarnation.renamed(self.snapshot.canonical_name(target.incarnation.name)))
            for target in self.wait.targets
        )
        inactive = tuple(
            target for target in targets
            if not self.agents.is_working(target.incarnation, self.snapshot)
        )
        return StandbyExecution(self.goal.id, targets, inactive)

    def paused(self, source: PauseSource) -> GoalExecutionControls:
        return PausedExecution(self.goal.id, source)

    def blocked(self, reason: str) -> GoalExecutionControls:
        return RetryableBlockExecution(self.goal.id, reason)

    def completed(self) -> GoalExecutionControls:
        return CompletedExecution(self.goal.id)

    def turn_execution(self) -> GoalExecutionControls:
        return self.owner.goal_turn_execution(self, self.turns)

    def external_turn_execution(self) -> GoalExecutionControls:
        if self.agents.is_working(self.owner.incarnation, self.snapshot):
            return RunningExecution(self.goal.id)
        return self.native_runnable()

    def unresolved(self) -> GoalExecutionControls:
        return AttemptUnresolvedExecution(self.goal.id)

    def native_runnable(self) -> GoalExecutionControls:
        return RunnableExecution(self.goal.id)

    def runnable(self) -> GoalExecutionControls:
        return self.owner.goal_runnable_execution(self)

    def failed(self, evidence: FailedTurnEvidence) -> GoalExecutionControls:
        return AttemptFailedExecution(self.goal.id, evidence)

    def failed_execution(self) -> GoalExecutionControls:
        return self.evidence.execution(self)

    def execution(self) -> GoalExecutionControls:
        return self.goal.state.execution(self)


class GoalSnapshotReader(
    GoalStoreAccess,
    GoalSnapshots[Thread, GoalSnapshot, RegistrySnapshot, ActivityAnswers],
):
    """Read-only projection of owners' current goals and scheduling states."""

    def snapshot_for(self, owner: Thread, snapshot: RegistrySnapshot,
                     wait: Callable[[GoalDeclaration], GoalWait]) -> GoalSnapshot:
        snapshot_of_owner, = self.snapshots_for((owner,), snapshot, wait, self.agents)
        return snapshot_of_owner

    def snapshots_for(self, owners: Iterable[Thread], snapshot: RegistrySnapshot,
                      wait: Callable[[GoalDeclaration], GoalWait],
                      activity: ActivityAnswers) -> tuple[GoalSnapshot, ...]:
        """All goal readers build the same source from the original records. Every owner's
        generation and failure evidence come from one cut of the goal store; permit
        attestation keeps GoalAttemptStore's independent fresh readback."""
        owners = tuple(owners)
        if not any(owner.goal for owner in owners):
            return tuple(GoalSnapshot(owner.goal, IdleGoalExecution()) for owner in owners)
        turns = TurnRecord.read_all(owner.turn_record for owner in owners if owner.goal)
        with self.open_goal_store().reader_snapshot() as connection:
            generations = Generation.require_all_in(
                connection, (goal.id for owner in owners for goal in owner.goal),
            )
            return tuple(self._snapshot_in(
                lambda: connection, owner, snapshot, wait, activity, turns, generations,
            ) for owner in owners)

    @staticmethod
    def _snapshot_in(connection: Callable[[], sqlite3.Connection], owner: Thread, snapshot: RegistrySnapshot,
                     wait: Callable[[GoalDeclaration], GoalWait], activity: ActivityAnswers,
                     turns: Mapping[StoredTurnRecord, TurnDeclaration],
                     generations: Mapping[GoalId, Generation]) -> GoalSnapshot:
        for goal in owner.goal:
            generation = generations[goal.id]
            source = GoalExecutionSource(
                goal=goal, wait=wait(goal), generation=generation,
                evidence=generation.lifecycle.evidence(connection(), generation),
                owner=owner, snapshot=snapshot, agents=activity, turns=turns,
            )
            return GoalSnapshot(goal, source.execution())
        return GoalSnapshot(owner.goal, IdleGoalExecution())
