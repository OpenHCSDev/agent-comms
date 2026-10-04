"""Owner-local goal scheduling/control over the sole durable attempt ledger.

The runner owns turn lifetimes. This owner holds goal origin and projection state,
and asks declared admission rules before recovering any unused READY grant.
"""

from __future__ import annotations

import os
from functools import partial
from typing import TYPE_CHECKING

from .child_process import ProcessIdentity
from .coordination_errors import StaleFence
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
from .goal_attempts import (
    Generation,
    GoalAttemptError,
    GoalAttemptStore,
    StaleAttemptError,
    UnresolvedAttemptError,
)
from .goal_presentation import GoalExecution
from .goals import Goal, GoalRevision
from .native_input_owner import GoalLaunchOwner
from .reservation_rules import ReservationViolationError
from .routing import ScheduledTurn
from .schedule_rules import GoalScheduleCheck, WakeScheduleCheck
from .store_files import _store_lock
from .threads import Thread

if TYPE_CHECKING:
    from .comms import Comms
    from .input_drain import InputDrain
    from .session_lifecycle import SessionLifecycle
    from .turn_effects import TurnEffects

GOAL_CONTINUE_PROMPT = "Continue working toward the active goal."


class GoalScheduler:
    def __init__(self, comms: Comms, effects: TurnEffects):
        self.comms = comms
        self.effects = effects
        self.goal_store: GoalAttemptStore | None = None
        self.pending_goal_origins: dict[str, str] = {}
        self.goal_execution_signatures: dict[str, tuple[Goal | None, GoalExecution | None]] = {}

    def bind(self, sessions: SessionLifecycle, inputs: InputDrain) -> None:
        self.sessions = sessions
        self.inputs = inputs

    async def schedule_goal(self, session_id: str) -> None:
        """Join durable goal admission before binding its existing loop queue."""
        check = GoalScheduleCheck(
            session_id=session_id, inputs=self.inputs, turns=self.effects.turns,
        )
        try:
            check.require_valid()
        except ReservationViolationError:
            return
        scheduled = await Coordination.run_worker(partial(self.prepare_goal, session_id))
        if scheduled is None:
            return
        # A worker wait cannot reserve the process-local queue. Its original
        # rules recheck that resource before the admitted work is bound.
        try:
            check.require_valid()
        except ReservationViolationError:
            return
        self.inputs.pending_turns.setdefault(session_id, []).append(scheduled)
        WakeScheduleCheck(session_id=session_id, inputs=self.inputs).schedule()

    def prepare_goal(self, session_id: str) -> ScheduledTurn | None:
        """Read/recover the original READY grant in its joined storage worker."""
        snapshot = self.comms.registry.snapshot()
        thread = snapshot.require(self.sessions.require(session_id))
        try:
            thread.require_local_process(ProcessIdentity.capture(os.getpid()))
            snapshot.status(thread.name).require_running()
            thread.require_idle()
        except RelationViolationError:
            return
        goal = thread.active_goal
        if goal is not None:
            if self.comms.goals.goal_wait(thread.name) is not None:
                return
            if self.pending_goal_origins.get(thread.name) == goal.id:
                return
            store = self.goal_store
            if (
                store is None
                and (self.comms.root / "goal-private" / "goal_attempts.sqlite3").exists()
            ):
                store = self.open_goal_store()
            if store is None:
                self.comms.goals.block_goal_after_failed_turn(
                    thread.name,
                    started_goal=goal,
                    expected_worktree=thread.worktree,
                    diagnostic="Goal launch grant unavailable; explicit Retry required.",
                )
                return
            try:
                generation = store.snapshot(goal.id)
                if generation is None:
                    raise UnresolvedAttemptError("Goal generation is absent")
                generation.lifecycle.require_ready()
                admission = self.comms.registry.snapshot().admission_generations[thread.name]
                with _store_lock(self.comms._wire_lock_path):
                    self.ready_goal_grant_locked(thread, admission, store, generation)
            except StaleAttemptError:
                return
            except UnresolvedAttemptError:
                self.comms.goals.block_goal_after_failed_turn(
                    thread.name, started_goal=goal, expected_worktree=thread.worktree,
                    diagnostic="Goal attempt unresolved; inspect diagnostics before Retry.",
                )
                return
            except GoalAttemptError:
                self.comms.goals.block_goal_after_failed_turn(
                    thread.name,
                    started_goal=goal,
                    expected_worktree=thread.worktree,
                    diagnostic="Goal launch grant unavailable; explicit Retry required.",
                )
                return
            return ScheduledTurn(GOAL_CONTINUE_PROMPT, goal_id=goal.id)

    def open_goal_store(self) -> GoalAttemptStore:
        if self.goal_store is None:
            private = self.comms.root / "goal-private"
            private.mkdir(mode=0o700, exist_ok=True)
            self.goal_store = GoalAttemptStore.initialize(private)
        return self.goal_store

    def ready_goal_grant_locked(
        self, owner: Thread, admission: int, store: GoalAttemptStore, generation: Generation
    ) -> str:
        """Caller holds the wire lock; READY recovery never authorizes an old owner."""
        try:
            GoalLaunchOwner(thread=owner, admission_generation=admission).require_ready(
                self.comms.registry.snapshot(), generation.goal_id,
            )
        except (RelationViolationError, ValueError, StaleFence) as error:
            raise StaleAttemptError(
                "The executing goal owner changed before READY recovery."
            ) from error
        try:
            return store.ready_grant(generation.goal_id, generation.number)
        except UnresolvedAttemptError:
            store.recover_unreserved_ready(generation.goal_id, generation.number)
            return store.ready_grant(generation.goal_id, generation.number)

    async def set_goal(self, session_id: str, text: str) -> Goal:
        """Commit a UI goal through its executing owner and private launch ledger."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("A goal requires text.")
        name = self.sessions.require(session_id)
        goal = await Coordination.run_worker(lambda: self.comms.goals.update_goal(
            name,
            SetGoalAction(text=text, expect=GoalPrecondition(expected_owner=ProcessIdentity.capture(os.getpid()))),
            actor=OwnerInvocable,
            owner_store=self.open_goal_store(),
        ))
        assert goal is not None
        await self.schedule_goal(session_id)
        return goal

    async def edit_goal(
        self, session_id: str, goal_id: str, expected_revision: int, text: str
    ) -> Goal:
        """Edit the current objective without replacing its identity or execution state."""
        name = self.sessions.require(session_id)
        thread = await Coordination.run_worker(partial(self.comms.registry.require, name))
        goal = thread.require_goal_checkpoint(GoalRevision(goal_id, expected_revision))
        # update_goal owns the wire lock and atomically rechecks both this
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
        assert edited is not None
        await self.sessions.config.sync_thread(session_id)
        return edited

    async def update_goal(
        self, session_id: str, action: type[GoalAction], goal_id: str, expected_revision: int
    ) -> Goal | None:
        """Apply an explicit UI pause, resume, or clear through the current owner."""
        if not issubclass(action, OwnerControlInvocable):
            raise ValueError("Goal updates support only active, paused, or clear.")
        name = self.sessions.require(session_id)
        thread = await Coordination.run_worker(partial(self.comms.registry.require, name))
        goal = thread.require_goal_checkpoint(GoalRevision(goal_id, expected_revision))
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
                owner_store=self.open_goal_store() if action.owner_grant else None,
            ))
        finally:
            # Resume can discover that a paused attempt failed. Publish the
            # reconciled BLOCKED state even when the action returns an error.
            await self.sessions.config.sync_thread(session_id)
        if action.schedules_goal:
            await self.schedule_goal(session_id)
        return updated

    async def retry_goal(self, session_id: str, goal_id: str, expected_revision: int) -> Goal:
        """Record an explicit UI retry in the executing owner's private ledger."""
        name = self.sessions.require(session_id)
        thread = await Coordination.run_worker(partial(self.comms.registry.require, name))
        goal = thread.require_goal_checkpoint(GoalRevision(goal_id, expected_revision))
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
            owner_store=self.open_goal_store(),
        ))
        assert resumed is not None
        # READY records the accepted owner decision even during an unrelated
        # turn. The scheduler's existing busy fences defer launch until that
        # turn finishes; reserved/claimed attempts remain unretryable above.
        await self.schedule_goal(session_id)
        await self.sync_goal_execution(session_id, name)
        return resumed

    async def sync_goal_execution(self, session_id: str, thread_name: str) -> None:
        event = await Coordination.run_worker(partial(
            self.comms.goals.goal_changed,
            thread_name, self.goal_execution_signatures.get(session_id),
        ))
        if event is None:
            return
        await self.effects._emit_event(session_id, event)
        self.goal_execution_signatures[session_id] = event.signature
