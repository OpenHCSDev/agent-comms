"""One turn's goal grants, provider charges and terminal settlement.

The existing SQLite attempt store remains the durable authority. This account
owns only the live turn's permits and observations, never a reconstructed grant.
"""

from __future__ import annotations

from contextlib import ExitStack, suppress
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

from acp import RequestError

from . import agent_events as events
from .diagnostics import terminal_failure_reason
from .goal_actions import BlockedGoalAction, GoalPrecondition
from .goal_attempts import (
    GoalAttemptError, GoalAttemptStore, GoalTurnPermit, LaunchPermit, NoLaunchPermit,
    StaleAttemptError,
)
from .goal_failure_observation import FailedTurnObservation
from .goal_states import ActiveGoal, CompletedGoal, PausedGoal
from .mro_dispatch import MroDispatch, handles
from .store_files import _store_lock

if TYPE_CHECKING:
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


@dataclass(frozen=True)
class FailedGoalOrigin(MroDispatch):
    comms: Comms
    thread_name: str
    goal: Goal

    @handles(ActiveGoal, CompletedGoal)
    def block(self, state) -> None:
        self.comms.goals.update_goal(
            self.thread_name,
            BlockedGoalAction(
                expect=GoalPrecondition(goal_id=self.goal.id, expected_goal=self.goal),
                progress="Goal origin turn did not finish successfully.",
            ),
        )


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
    productive_tool: events.ToolEnd | None = field(default=None, init=False)

    @staticmethod
    def reserve(*, owner: Thread, goal: Goal, comms: Comms, autonomous: bool) -> GoalTurnPermit:
        try:
            store = comms.goals.open_goal_store()
            claim_scope = comms.registry.snapshot().claimed_generations[owner.name]
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
        goal_id = self.comms.registry.require(self.thread_name).goal.reported_id()
        permit = self.originated.get(goal_id, self.permit)
        if not permit.charge_for(goal_id, self.comms.goals, str(event.response_id), event.usage):
            self.unattributed_usage.append((str(event.response_id), event.usage))

    def tool_ended(self, event: events.ToolEnd) -> None:
        if event.ok:
            self.productive_tool = event
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

    def block_active_goal(self, diagnostic: str) -> None:
        current = self.comms.registry.require(self.thread_name).continuation_goal(self.owner)
        current.block_after_failed_turn(
            self.comms.goals, self.thread_name, self.owner.worktree, diagnostic,
        )

    def done(self, event: events.Done) -> None:
        if not event.ok:
            self.permit.block(self, "Backend turn failed; inspect local diagnostics before resuming.")
        elif self.productive_tool is None and not str(event.text or "").strip():
            self.permit.block(self,
                "Backend reported success without assistant output "
                "or tool activity; inspect the session before resuming."
            )

    def settle_launched(self, permit: LaunchPermit, terminal: events.Done | None) -> None:
        if terminal is None:
            self.block_active_goal("Backend turn ended without a result; inspect local diagnostics.")
        store = self.comms.goals.open_goal_store()
        captured = self.owner.goal.require().require_id(permit.reservation.goal_id)
        current = captured.continued_by(self.comms.registry.require(self.thread_name).goal)
        if terminal is not None and terminal.ok:
            current.settle(VerifiedGoalSettlement, store, permit, self.turn)
        if permit.has_verified_progress(store):
            return
        snapshot = self.comms.registry.snapshot()
        observation = None
        if terminal is None or not terminal.ok:
            observation = FailedTurnObservation.from_terminal(
                permit.reservation,
                owner=self.owner,
                goal=captured,
                lease=self.lease,
                turn_id=self.turn.value,
                claim_scope=self.lease.claimed_generation,
                current_owner=snapshot.threads.get(self.thread_name),
                current_claim=snapshot.claimed_generations.get(self.thread_name),
                reason=terminal_failure_reason(asdict(terminal) if terminal is not None else {}),
            )
        with suppress(StaleAttemptError):
            permit.reservation.fail(
                store,
                "Goal turn ended without verified terminal progress.",
                observation=observation,
            )
        current.block_after_failed_turn(
            self.comms.goals, self.thread_name, self.owner.worktree,
            "Goal turn ended without verified terminal progress.",
        )

    def finish(self, terminal: events.Done | None) -> None:
        """Settle all original grants once from the actual producer terminal event."""
        self.permit.settle(self, terminal)
        for goal_id, permit in self.originated.items():
            current = self.comms.registry.require(self.thread_name).goal.matching(goal_id)
            store = self.comms.goals.open_goal_store()
            if terminal is not None and terminal.ok:
                permit.settle_goal(OriginGoalSettlement, store, current, self.turn)
            if not permit.has_verified_progress(store):
                permit.fail_origin(store, goal_id)
                for goal in current:
                    FailedGoalOrigin(self.comms, self.thread_name, goal).dispatch_sync(goal.state)
            if self.pending_origins.get(self.thread_name) == goal_id:
                self.pending_origins.pop(self.thread_name, None)
