"""Owner-local goal scheduling/control over the sole durable attempt ledger.

The runner owns turn lifetimes. This owner holds goal origin and projection state,
and checks the declared start rules before recovering any unused READY grant.
"""

from __future__ import annotations

from agent_comms.field_codec import GoalId
from .errors import GoalAttemptError
import asyncio
import os
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING

from .agent_event_updates import AcpEventConsumer
from .child_process import ProcessIdentity
from .coordinator import Coordination
from .errors import RelationViolationError
from .goal_actions import (
    EditGoalAction,
    GoalAction,
    GoalPrecondition,
    OwnerControlInvocable,
    OwnerInvocable,
    RetryGoalAction,
    SetGoalAction,
)
from .goal_presentation import GoalPublicationSnapshot, UnpublishedGoalSnapshot
from .goals import Goal, GoalDeclaration, GoalRevision
from .reservation_rules import ReservationViolationError
from .routing import ScheduledTurn
from .schedule_rules import OwnerTurnScheduleCheck, WakeScheduleCheck

if TYPE_CHECKING:
    from .registry_document import RegistrySnapshot
    from .threads import Thread
    from .comms import Comms
    from .input_drain import InputDrain
    from .session_lifecycle import SessionLifecycle
    from .turn_effects import TurnEffects

GOAL_CONTINUE_PROMPT = "Continue working toward the active goal."


@dataclass(eq=False, slots=True)
class GoalSessionPublication:
    """One session's last published goal and its publication lifetime."""

    snapshot: GoalPublicationSnapshot = field(default_factory=UnpublishedGoalSnapshot)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def in_standby(self) -> bool:
        return self.snapshot.standby()


class GoalScheduler:
    def __init__(self, comms: Comms, effects: TurnEffects):
        self.comms = comms
        self.effects = effects
        self.pending_goal_origins: dict[str, GoalId] = {}
        self.publications: dict[str, GoalSessionPublication] = {}

    def publication(self, session_id: str) -> GoalSessionPublication:
        return self.publications.setdefault(session_id, GoalSessionPublication())

    def bind(self, sessions: SessionLifecycle, inputs: InputDrain) -> None:
        self.sessions = sessions
        self.inputs = inputs

    async def _retain_current_goal_turns(self, session_id: str) -> None:
        """Keep only turns still owned by the currently active goal."""
        current = await Coordination.run_worker(lambda: self.comms.registry.require(
            self.sessions.require(session_id)
        ).goal.active())
        WakeScheduleCheck._retain_goal_turns(
            self.inputs.state(session_id).pending_turns, current,
        )

    async def schedule_goal(self, session_id: str) -> None:
        """Finish recording the goal's start permission before binding its loop queue."""
        await self._retain_current_goal_turns(session_id)
        check = OwnerTurnScheduleCheck(
            session_id=session_id, inputs=self.inputs, turns=self.effects.turns,
        )
        try:
            check.require_valid()
        except ReservationViolationError:
            return
        scheduled = await Coordination.run_worker(partial(self.prepare_goal, session_id))
        if scheduled is None:
            # The goal may have changed while its storage worker was preparing.
            await self._retain_current_goal_turns(session_id)
            return
        # A worker wait cannot reserve the process-local queue. Its original
        # rules recheck that resource before the authorized work is bound.
        try:
            check.require_valid()
        except ReservationViolationError:
            return
        self.inputs.state(session_id).pending_turns.append(scheduled)
        WakeScheduleCheck(session_id=session_id, inputs=self.inputs).schedule()

    def prepare_goal(self, session_id: str) -> ScheduledTurn | None:
        """Read/recover the original READY grant in its joined storage worker."""
        with self.comms.registry.store.locked():
            snapshot = self.comms.registry.snapshot()
            thread = snapshot.require(self.sessions.require(session_id))
            try:
                thread.require_local_process(ProcessIdentity.capture(os.getpid()))
                snapshot.status(thread.name).require_running()
                thread.turn_record.read().require_idle()
            except RelationViolationError:
                return
            try:
                scheduled = (turn for goal in thread.goal.active()
                             for turn in self._prepare_declared_goal(goal, thread, snapshot))
                return next(iter(scheduled), None)
            except GoalAttemptError:
                return

    def _prepare_declared_goal(self, goal: Goal, thread: Thread,
                               snapshot: RegistrySnapshot) -> tuple[ScheduledTurn, ...]:
        if not self.comms.goals.goal_wait(thread.name).allows_routed_input():
            return ()
        if self.pending_goal_origins.get(thread.name) == goal.id:
            return ()
        store = self.comms.goals.open_goal_store()
        generation = store.require_generation(goal.id)
        generation.lifecycle.require_ready()
        self.comms.goals.ready_goal_grant_locked(thread, snapshot.claim_generation(thread.name), generation)
        return (ScheduledTurn(GOAL_CONTINUE_PROMPT, goal_id=goal.id),)

    async def set_goal(self, session_id: str, text: str) -> Goal:
        """Commit a UI goal through its executing owner and private launch ledger."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("A goal requires text.")
        name = self.sessions.require(session_id)
        goal = await Coordination.run_worker(lambda: self.comms.goals.update_goal(
            name,
            SetGoalAction(text=text, expect=GoalPrecondition(expected_owner=ProcessIdentity.capture(os.getpid()))),
            actor=OwnerInvocable,
        ))
        await self.schedule_goal(session_id)
        return goal

    async def edit_goal(
        self, session_id: str, goal_id: GoalId, expected_revision: int, text: str
    ) -> Goal:
        """Edit the current objective without replacing its identity or execution state."""
        name = self.sessions.require(session_id)
        thread = await Coordination.run_worker(partial(self.comms.registry.require, name))
        goal = thread.goal.require_checkpoint(GoalRevision(goal_id, expected_revision))
        # update_goal owns the registry lock and atomically rechecks both this
        # snapshot and the executing owner. Do not acquire its lock twice.
        edited = await Coordination.run_worker(lambda: self.comms.goals.update_goal(
            name,
            EditGoalAction(
                text=text,
                expect=GoalPrecondition(
                    goal_id=goal_id,
                    expected_goal=goal,
                    expected_owner=ProcessIdentity.capture(os.getpid()),
                ),
            ),
            actor=OwnerInvocable,
        ))
        await self.sessions.config.apply_stored_settings(session_id)
        return edited

    async def update_goal(
        self, session_id: str, action: type[GoalAction], goal_id: GoalId, expected_revision: int
    ) -> GoalDeclaration:
        """Apply an explicit UI pause, resume, or clear through the current owner."""
        if not issubclass(action, OwnerControlInvocable):
            raise ValueError("Goal updates support only active, paused, or clear.")
        name = self.sessions.require(session_id)
        thread = await Coordination.run_worker(partial(self.comms.registry.require, name))
        goal = thread.goal.require_checkpoint(GoalRevision(goal_id, expected_revision))
        try:
            updated = await Coordination.run_worker(lambda: self.comms.goals.update_goal(
                name,
                action(
                    expect=GoalPrecondition(
                        goal_id=goal_id,
                        expected_goal=goal,
                        expected_owner=ProcessIdentity.capture(os.getpid()),
                    )
                ),
                actor=OwnerInvocable,
            ))
        finally:
            # Resume can discover that a paused attempt failed. Publish the
            # reconciled BLOCKED state even when the action returns an error.
            await self.sessions.config.apply_stored_settings(session_id)
        if action.schedules_goal:
            await self.schedule_goal(session_id)
        return updated

    async def retry_goal(self, session_id: str, goal_id: GoalId, expected_revision: int) -> Goal:
        """Record an explicit UI retry in the executing owner's private ledger."""
        name = self.sessions.require(session_id)
        thread = await Coordination.run_worker(partial(self.comms.registry.require, name))
        goal = thread.goal.require_checkpoint(GoalRevision(goal_id, expected_revision))
        if self.pending_goal_origins.get(name) == goal_id:
            raise ValueError("Wait for the goal origin turn to finish.")
        resumed = await Coordination.run_worker(lambda: self.comms.goals.update_goal(
            name,
            RetryGoalAction(
                expect=GoalPrecondition(
                    goal_id=goal_id,
                    expected_goal=goal,
                    expected_owner=ProcessIdentity.capture(os.getpid()),
                )
            ),
            actor=OwnerInvocable,
        ))
        assert resumed is not None
        # READY records the accepted owner decision even during an unrelated
        # turn. The scheduler's existing busy fences defer launch until that
        # turn finishes; reserved/claimed attempts remain unretryable above.
        await self.schedule_goal(session_id)
        await self.sync_goal_execution(session_id, name)
        return resumed

    def in_standby(self, session_id: str) -> bool:
        return self.publication(session_id).in_standby()

    async def sync_goal_execution(self, session_id: str, thread_name: str) -> None:
        # A publication advances this session's snapshot only after delivery.
        record = self.publication(session_id)
        async with record.lock:
            event = await Coordination.run_worker(partial(
                self.comms.goals.goal_changed, thread_name, record.snapshot,
            ))
            record.snapshot = await event.publish(
                AcpEventConsumer(self.effects, session_id, self.effects._runtime), record.snapshot
            )
