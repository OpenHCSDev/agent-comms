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
from .goal_attempts import GoalAttemptError, GoalAttemptStore, LaunchPermit, StaleAttemptError
from .goal_failure_observation import FailedTurnObservation
from .goals import GoalDeclaration, NoGoal
from .goal_states import ActiveGoal, CompletedGoal, PausedGoal
from .mro_dispatch import MroDispatch, handles
from .store_files import _store_lock

if TYPE_CHECKING:
    from .comms import Comms
    from .goals import Goal
    from .pi_payloads import PiUsage
    from .thread_identity import TurnId
    from .threads import Thread
    from .turn_lease import TurnLeaseFence


@dataclass
class VerifiedGoalSettlement(MroDispatch):
    store: GoalAttemptStore
    permit: LaunchPermit | None
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
        if self.permit is not None:
            self.permit.record_verified_progress(self.store, self.progress_witness)

    @handles(CompletedGoal)
    def completion(self, state: CompletedGoal) -> None:
        if self.permit is not None and self.goal.reported_turn == self.turn.value:
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
    permit: LaunchPermit | None
    pending_origins: dict[str, str]
    claims: ExitStack
    originated: dict[str, LaunchPermit | None] = field(default_factory=dict, init=False)
    unattributed_usage: list[tuple[str, PiUsage]] = field(default_factory=list, init=False)
    productive_tool: events.ToolEnd | None = field(default=None, init=False)

    @staticmethod
    def reserve(*, goal, owner, comms, autonomous):
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
                return None
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
        self.comms.registry.require(self.thread_name).goal.record_usage(self, event)

    def record_goal_usage(self, goal: Goal, event: events.ProviderUsage) -> None:
        permit = self.originated.get(goal.id)
        if (
            permit is None
            and self.permit is not None
            and goal.id == self.permit.reservation.goal_id
        ):
            permit = self.permit
        if permit is not None:
            permit.record_provider_usage(
                self.comms.goals.open_goal_store(), str(event.response_id), event.usage.to_wire()
            )
        else:
            self.unattributed_usage.append((str(event.response_id), event.usage))

    def tool_ended(self, event: events.ToolEnd) -> None:
        if event.ok:
            self.productive_tool = event
        if event.name != "comms_set_goal" or event.ok is not True:
            return
        with ExitStack() as completions:
            with _store_lock(self.comms._wire_lock_path):
                owner = self.comms.registry.snapshot().require(self.owner.name)
                owner.goal.claim_origin(self, owner, completions)

    def claim_goal_origin(self, goal: Goal, owner: Thread, completions: ExitStack) -> None:
        if goal.id in self.originated:
            return
        store = self.comms.goals.open_goal_store()
        self.originated[goal.id] = None
        generation = store.require_generation(goal.id)
        generation.lifecycle.require_ready()
        grant = self.comms.goals.ready_goal_grant_locked(
            owner, self.lease.claimed_generation, generation,
        )
        permit = store.claim_launch(store.reserve(goal.id, generation.number, ready_grant=grant))
        completions.callback(self.finish_goal_origin, goal, store, permit)

    def finish_goal_origin(self, goal: Goal, store: GoalAttemptStore, permit: LaunchPermit) -> None:
        self.claims.callback(permit.retire_unverified, store)
        self.originated[goal.id] = permit
        for response_id, usage in self.unattributed_usage:
            permit.record_provider_usage(store, response_id, usage.to_wire())
        self.unattributed_usage.clear()
        self.pending_origins[self.thread_name] = goal.id

    def block_current(self, diagnostic: str) -> None:
        self.comms.goals.block_goal_after_failed_turn(
            self.thread_name,
            started_goal=self.owner.goal,
            expected_worktree=self.owner.worktree,
            diagnostic=diagnostic,
        )

    def current_active_goal(self) -> GoalDeclaration:
        if self.permit is None:
            return NoGoal()
        owner = self.comms.registry.require(self.thread_name)
        return owner.continuation_goal(self.owner)

    def done(self, event: events.Done) -> None:
        self.current_active_goal().check_turn_result(self, event)

    def settle_original(self, terminal: events.Done | None) -> None:
        if self.permit is None:
            return
        if terminal is None:
            self.current_active_goal().block_turn_account(
                self, "Backend turn ended without a result; inspect local diagnostics.",
            )
        store = self.comms.goals.open_goal_store()
        current = self.comms.registry.require(self.thread_name).goal.matching(self.owner.goal.require().id)
        if terminal is not None and terminal.ok:
            current.settle_turn(VerifiedGoalSettlement, store, self.permit, self.turn)
        if self.permit.has_verified_progress(store):
            return
        snapshot = self.comms.registry.snapshot()
        observation = None
        if terminal is None or not terminal.ok:
            observation = FailedTurnObservation.from_terminal(
                self.permit.reservation,
                owner=self.owner,
                goal=self.owner.goal,
                lease=self.lease,
                turn_id=self.turn.value,
                claim_scope=self.lease.claimed_generation,
                current_owner=snapshot.threads.get(self.thread_name),
                current_claim=snapshot.claimed_generations.get(self.thread_name),
                reason=terminal_failure_reason(asdict(terminal) if terminal is not None else {}),
            )
        with suppress(StaleAttemptError):
            self.permit.reservation.fail(
                store,
                "Goal turn ended without verified terminal progress.",
                observation=observation,
            )
        current.block_turn_account(self, "Goal turn ended without verified terminal progress.")

    def finish(self, terminal: events.Done | None) -> None:
        """Settle all original grants once from the actual producer terminal event."""
        self.settle_original(terminal)
        for goal_id, permit in self.originated.items():
            current = self.comms.registry.require(self.thread_name).goal.matching(goal_id)
            store = self.comms.goals.open_goal_store()
            if terminal is not None and terminal.ok:
                current.settle_turn(OriginGoalSettlement, store, permit, self.turn)
            if permit is None or not permit.has_verified_progress(store):
                if permit is not None:
                    with suppress(StaleAttemptError):
                        permit.reservation.fail(
                            store, "Goal origin turn did not finish successfully."
                        )
                else:
                    generation = store.snapshot(goal_id)
                    if generation is not None and generation.lifecycle.allows_resume(True):
                        store.retire_goal(
                            goal_id,
                            expected_generation=generation.number,
                            attempt_id=generation.attempt_id,
                        )
                current.fail_origin(self.comms, self.thread_name)
            if self.pending_origins.get(self.thread_name) == goal_id:
                self.pending_origins.pop(self.thread_name, None)
