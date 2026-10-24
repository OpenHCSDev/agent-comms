"""One turn's goal grants, provider charges and terminal settlement.

The existing SQLite attempt store remains the durable authority. This account
owns only the live turn's permits and observations, never a reconstructed grant.
"""

from __future__ import annotations

from agent_comms.errors import GoalAttemptError
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agent_comms.coordination_errors import RequestRefused

from .field_codec import TurnId
from agent_comms.goal_turn_permit import GoalTurnPermit, NoLaunchPermit
from .goal_states import ActiveGoal, CompletedGoal, PausedGoal
from .mro_dispatch import MroDispatch, handles

if TYPE_CHECKING:
    from .goal_attempts import GoalAttemptStore, LaunchPermit
    from . import agent_events as events
    from .turn_terminal import TurnTerminal
    from .comms import Comms
    from .goals import Goal, GoalDeclaration
    from .pi_payloads import PiUsage
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
        comms, owner = account.comms, account.owner
        with comms.registry.store.locked():
            current = comms.registry.require(account.thread_name)
            if current.incarnation != owner.incarnation or current.worktree != owner.worktree:
                return
            for goal in owner.goal.continued_by(current.goal):
                store = comms.goals.open_goal_store()
                generation = store.require_generation(goal.id)
                generation.lifecycle.continue_work(
                    store, generation, f"verified-turn:{account.turn}"
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
        if self.goal.reports_turn(self.turn):
            self.permit.record_verified_completion(self.store, self.completion_witness)


class OriginGoalSettlement(VerifiedGoalSettlement):
    @property
    def progress_witness(self) -> str:
        return f"origin-final:{self.turn}"

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
            with comms.registry.store.locked():
                claim_scope = comms.registry.snapshot().claim_generation(owner.name)
                generation = store.require_generation(goal.id)
                return generation.lifecycle.turn_permit(
                    store, comms.goals, owner, claim_scope, generation
                )
        except GoalAttemptError as error:
            if autonomous:
                return NoLaunchPermit()
            raise RequestRefused.invalid_params(
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
        with self.comms.registry.store.locked():
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
