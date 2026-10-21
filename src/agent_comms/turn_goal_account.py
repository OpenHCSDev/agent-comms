"""One turn's goal grants, provider charges and terminal settlement.

The existing SQLite attempt store remains the durable authority. This account
owns only the live turn's permits and observations, never a reconstructed grant.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from acp import RequestError

from agent_comms.goal_attempts import (
    GoalAttemptError, GoalAttemptStore, LaunchPermit,
)
from agent_comms.goal_turn_permit import GoalTurnPermit, NoLaunchPermit
from .goal_states import ActiveGoal, CompletedGoal, PausedGoal
from .mro_dispatch import MroDispatch, handles
from .store_files import _store_lock

if TYPE_CHECKING:
    from . import agent_events as events
    from .turn_terminal import TurnTerminal
    from .comms import Comms
    from .goals import Goal, GoalDeclaration
    from .pi_payloads import PiUsage
    from .thread_identity import TurnId
    from .threads import Thread
    from .turn_lease import TurnLeaseFence


@dataclass
class VerifiedGoalSettlement(MroDispatch):
    store: GoalAttemptStore
    permit: LaunchPermit
    goal: Goal
    turn: TurnId

    @classmethod
    def settle_current(cls, account: TurnGoalAccount) -> None:
        """Publish the state's verified successor, including a turn without a grant."""
        comms, owner = account.comms, account.owner
        with _store_lock(comms._wire_lock_path):
            current = comms.registry.require(account.thread_name)
            if current.incarnation != owner.incarnation or current.worktree != owner.worktree:
                return
            settled = current.goal.after_verified_turn(owner.goal, account.turn)
            if settled is not current.goal:
                comms.registry.register(replace(current, goal=settled), comms.registry.status(current.name))

    @property
    def progress_witness(self) -> str:
        return f"native-terminal:{self.turn.value}"

    @property
    def completion_witness(self) -> str:
        return f"registry-revision:{self.goal.revision}"

    @handles(ActiveGoal, PausedGoal)
    def progress(self, state) -> None:
        # An owner pause prevents another launch, not recording verified work.
        self.permit.record_verified_progress(self.store, self.progress_witness)

    @handles(CompletedGoal)
    def completion(self, state: CompletedGoal) -> None:
        if self.goal.reports_turn(self.turn.value):
            self.permit.record_verified_completion(self.store, self.completion_witness)


class OriginGoalSettlement(VerifiedGoalSettlement):
    @property
    def progress_witness(self) -> str:
        return f"origin-final:{self.turn.value}"

    @property
    def completion_witness(self) -> str:
        return f"origin-completed:{self.goal.revision}"


@dataclass(kw_only=True)
class TurnGoalAccount:
    comms: Comms
    owner: Thread
    turn: TurnId
    lease: TurnLeaseFence
    permit: GoalTurnPermit
    pending_origins: dict[str, str]
    claims: ExitStack
    originated: dict[str, GoalTurnPermit] = field(default_factory=dict, init=False)
    unattributed_usage: list[tuple[str, PiUsage]] = field(default_factory=list, init=False)
    has_productive_tool: bool = field(default=False, init=False)

    @staticmethod
    def reserve(*, owner: Thread, goal: Goal, comms: Comms, autonomous: bool) -> GoalTurnPermit:
        try:
            store = comms.goals.open_goal_store()
            claim_scope = comms.registry.snapshot().claim_generation(owner.name)
            with _store_lock(comms._wire_lock_path):
                generation = store.require_generation(goal.id)
                generation.lifecycle.require_ready()
                grant = comms.goals.ready_goal_grant_locked(owner, claim_scope, generation)
                reservation = store.reserve(goal.id, generation.number, ready_grant=grant)
                return store.claim_launch(reservation)
        except GoalAttemptError as error:
            if autonomous:
                return NoLaunchPermit()
            raise RequestError.invalid_params(
                {
                    "reason": "goal_attempt_unavailable",
                    "details": "The goal has no launchable attempt. Inspect its state and use "
                    "Retry for a failed attempt; no prompt was sent to Pi.",
                }
            ) from error

    @property
    def thread_name(self) -> str:
        snapshot = self.comms.registry.snapshot()
        return snapshot.canonical_name(self.owner.name)

    def provider_usage(self, event: events.ProviderUsage) -> None:
        for goal in self.comms.registry.require(self.thread_name).goal:
            permit = self.originated.get(goal.id, self.permit)
            if permit.charge_for(goal.id, self.comms.goals, str(event.response_id), event.usage):
                return
        self.unattributed_usage.append((str(event.response_id), event.usage))

    def tool_ended(self, event: events.ToolEnd) -> None:
        self.has_productive_tool |= bool(event.ok)
        if event.name != "comms_set_goal" or event.ok is not True:
            return
        with _store_lock(self.comms._wire_lock_path):
            owner = self.comms.registry.snapshot().require(self.owner.name)
            claimed = tuple(permit for goal in owner.goal for permit in self._reserve_origin(owner, goal))
        for permit in claimed:
            self._record_origin_usage(permit)

    def _reserve_origin(self, owner: Thread, goal: Goal) -> tuple[LaunchPermit, ...]:
        if goal.id in self.originated:
            return ()
        store = self.comms.goals.open_goal_store()
        self.originated[goal.id] = NoLaunchPermit()
        generation = store.require_generation(goal.id)
        generation.lifecycle.require_ready()
        grant = self.comms.goals.ready_goal_grant_locked(
            owner, self.lease.claimed_generation, generation,
        )
        permit = store.claim_launch(
            store.reserve(goal.id, generation.number, ready_grant=grant)
        )
        self.claims.callback(permit.retire_unverified, store)
        self.originated[goal.id] = permit
        return (permit,)

    def _record_origin_usage(self, permit: LaunchPermit) -> None:
        store = self.comms.goals.open_goal_store()
        for response_id, usage in self.unattributed_usage:
            permit.record_provider_usage(store, response_id, usage)
        self.unattributed_usage.clear()
        self.pending_origins[self.thread_name] = permit.reservation.goal_id

    def settle_launched(self, permit: LaunchPermit, terminal: TurnTerminal) -> None:
        store = self.comms.goals.open_goal_store()
        captured = self.owner.goal.require().require_id(permit.reservation.goal_id)
        current = captured.continued_by(self.comms.registry.require(self.thread_name).goal)
        terminal.settle_launched(self, permit, store, captured, current)

    def finish(self, terminal: TurnTerminal) -> None:
        """Settle all original grants once from the actual producer terminal event."""
        terminal.settle_current(self)
        self.permit.settle(self, terminal)
        for goal_id, permit in self.originated.items():
            current = self.comms.registry.require(self.thread_name).goal.matching(goal_id)
            store = self.comms.goals.open_goal_store()
            terminal.settle_origin(self, permit, store, goal_id, current)
            if self.pending_origins.get(self.thread_name) == goal_id:
                self.pending_origins.pop(self.thread_name, None)
