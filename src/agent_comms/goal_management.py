"""Goal state, input reviews and wait lifecycle operations."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from contextlib import contextmanager, ExitStack
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .goal_actions import GoalAction, RuntimeInvocable
from .goal_attempts import Generation, GoalAttemptStore, StaleAttemptError, UnresolvedAttemptError
from .coordination_errors import StaleFence, PublicationUncertain
from .errors import RelationViolationError
from .native_input_owner import GoalLaunchOwner
from .goal_history import GoalHistoryEntry
from .bus_publication import stable_thread_lookup
from .goal_waits import GoalInputReview, GoalWait, GoalWaits
from .registration import Registration

if TYPE_CHECKING:
    from .agent_events import GoalChanged
    from .message_reference import MessageReference
    from .native_input_owner import RegistryOwner
from .goal_presentation import GoalExecution, GoalWaitTarget
from .goals import Goal
from .message_bus import MessageBus
from .store_files import _store_lock
from .threads import Thread
from .turn_lease import FinishedTurnFence

_LOG = logging.getLogger(__name__)


class Goals:
    def __init__(self, root: Path, registry: Registration, bus: MessageBus, agents):
        self.root = root
        self.registry = registry
        self.bus = bus
        self.agents = agents
        self._wire_lock_path = root / "wire"
        self.waits = GoalWaits(root / GoalWaits.filename)
        self.goal_store: GoalAttemptStore | None = None

    @contextmanager
    def writing(self, *, shared=False):
        # Publication runs after physical custody ends, also on exceptional exits.
        with ExitStack() as completed:
            with _store_lock(self._wire_lock_path, shared=shared):
                yield completed

    def _observation(self, snapshot, thread, event, at):
        from .fleet import UnitObservation

        owner = snapshot.owner_identity(thread.name)
        return UnitObservation(thread, self.agents._observed(thread, owner, event),
                               thread.execution.latest_tool(self.root, thread), at)

    def _previous_at(self, goal, event):
        return max((event.timestamp, *goal.work_time.timestamps,
                    *goal.report_time.timestamps, *goal.author.issued_times))

    def write_goal(self, thread, goal, completed):
        """The work-goal writer; callers hold writing() custody."""
        from .recorded_time import RecordedEventTime

        if goal is not None:
            goal = replace(goal, work_time=RecordedEventTime(time.time()))
        snapshot = self.registry.snapshot()
        event = self.agents.activity.current(thread.name, active=True)
        self.registry.register(replace(thread, goal=goal), self.registry.status(thread.name))
        if (goal is not None and goal.mention_source is not None
                and goal.author.notification_targets(snapshot)):
            before = self._observation(snapshot, thread, event,
                                       self._previous_at(thread.goal, event)
                                       if thread.goal is not None else event.timestamp)
            after = self._observation(snapshot, replace(thread, goal=goal), event,
                                      max(goal.work_time.timestamps))
            completed.callback(self._notify_transition, before, after)
        return goal

    def activity_written(self, snapshot, change):
        if change is None:
            return
        before_event, after_event = change
        thread = snapshot.threads[after_event.thread]
        goal = thread.goal
        if (goal is None or goal.mention_source is None
                or not goal.author.notification_targets(snapshot)):
            return
        from .fleet import UnitObservation
        from .activity import ObservedActivity

        # The original event owns the previous phase. Projecting it through
        # the just-finished native lease would rewrite busy history as idle.
        before = UnitObservation(
            thread, ObservedActivity.acquire(before_event, snapshot.owner_identity(thread.name)),
            thread.execution.latest_tool(self.root, thread), self._previous_at(goal, before_event),
        )
        after = self._observation(snapshot, thread, after_event, after_event.timestamp)
        self._notify_transition(before, after)

    def _notify_transition(self, before, after):
        from .goals import SentGoalNotice, UnconfirmedGoalNotice
        from .messages import Message, AlertKind

        progress = after.progress
        if not progress.notifies_commander or type(before.progress) is type(progress):
            return
        original = after.thread.incarnation
        with _store_lock(self._wire_lock_path):
            snapshot = self.registry.snapshot()
            if not original.current(snapshot):
                return
            unit = snapshot.require(original.name)
            for target in after.goal.author.notification_targets(snapshot):
                commander = snapshot.require(target.name)
                message = Message(
                    sender=unit.name, target=commander.name, type=AlertKind,
                    timestamp=after.observed_at,
                    body=f"{unit.name}: {progress.summary}. Goal {after.goal.id}: {after.goal.text}",
                )
                try:
                    # A Core notice must not implicitly answer the unit's input.
                    published = self.bus.publisher.publish_ordinary(message)
                    result = SentGoalNotice(published.reference)
                except PublicationUncertain as error:
                    result = UnconfirmedGoalNotice(target, f"{type(error).__name__}: {error}")
                    _LOG.exception("State saved; commander notice unconfirmed; no automatic resend")
                current = self.registry.require(unit.name)
                if current.goal is not None and current.goal.id == after.goal.id:
                    # Communication receipts do not reset the work clock.
                    self.registry.register(replace(current, goal=replace(
                        current.goal, notice=result, revision=current.goal.revision + 1,
                    )), self.registry.status(current.name))

    def open_goal_store(self) -> GoalAttemptStore:
        if self.goal_store is None:
            private = self.root / "goal-private"
            private.mkdir(mode=0o700, exist_ok=True)
            self.goal_store = GoalAttemptStore.initialize(private)
        return self.goal_store

    def ready_goal_grant_locked(
        self, owner: Thread, admission: int, generation: Generation
    ) -> str:
        """Caller holds wire; recover only this owner's original unused READY grant."""
        try:
            GoalLaunchOwner(thread=owner, admission_generation=admission).require_ready(
                self.registry.snapshot(), generation.goal_id,
            )
        except (RelationViolationError, ValueError, StaleFence) as error:
            raise StaleAttemptError(
                "The executing goal owner changed before READY recovery."
            ) from error
        store = self.open_goal_store()
        try:
            return store.ready_grant(generation.goal_id, generation.number)
        except UnresolvedAttemptError:
            store.recover_unreserved_ready(generation.goal_id, generation.number)
            return store.ready_grant(generation.goal_id, generation.number)

    def unresolved_inputs(self, name: str) -> list[dict[str, Any]]:
        """Project durable unresolved inputs; reading never schedules another attempt."""
        from .input_disposition import InputDispositions

        with _store_lock(self._wire_lock_path, shared=True):
            self.registry.require(name)
            rows = (
                InputDispositions(self.root)
                .unresolved(aliases := self.registry.aliases_for(name))
                .unknown(aliases)
            )
            return [row.public() for row in rows]

    def input_delivery(
        self,
        name: str,
        *,
        include_history: bool = False,
        awaiting_keys: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        """Project durable notices using current owner queue facts when available."""
        from .input_disposition import InputDispositions

        with _store_lock(self._wire_lock_path, shared=True):
            self.registry.require(name)
            aliases = self.registry.aliases_for(name)
            return (
                InputDispositions(self.root)
                .unresolved(aliases)
                .delivery_overview(
                    aliases, include_history=include_history, awaiting_keys=awaiting_keys
                )
            )

    def dismiss_historical_inputs(
        self, name: str, *, awaiting_keys: frozenset[str] | None = None
    ) -> dict[str, Any]:
        """Dismiss only historical notices; UNKNOWN remains unresolved and unreplayable."""
        from .input_disposition import InputDispositions

        with _store_lock(self._wire_lock_path):
            self.registry.require(name)
            aliases = self.registry.aliases_for(name)
            return InputDispositions(self.root).dismiss_historical(
                aliases, awaiting_keys=awaiting_keys
            )

    def goal_input_review(self, name: str, goal_id: str, wait_for: Sequence[str]) -> dict:
        """Project exact review eligibility for one current goal and dependency set."""
        with _store_lock(self._wire_lock_path, shared=True):
            return self._goal_input_review(self.registry.require(name), goal_id, wait_for).public()

    def _goal_input_review(
        self, thread: Thread, goal_id: str, wait_for: Sequence[str]
    ) -> GoalInputReview:
        from .input_disposition import InputDispositions

        goal = thread.require_active_goal(goal_id)
        if not wait_for:
            raise ValueError("Standby requires explicit wait_for thread names.")
        resolved = tuple(self.registry.require(target.removeprefix("@")) for target in wait_for)
        if any(target.created_at == thread.created_at for target in resolved):
            raise ValueError("A goal cannot wait for its own thread.")
        targets = tuple(
            dict.fromkeys(GoalWaitTarget(target.name, target.created_at) for target in resolved)
        )
        owners = self.registry.aliases_for(thread.name)
        dispositions = InputDispositions(self.root)
        unknown = {row.key: row for row in dispositions.unresolved(owners).unknown(owners)}
        eligible = set()
        snapshot = self.registry.snapshot()
        from .input_attempt import ReservedInput

        lookup = stable_thread_lookup(thread.created_at)
        # Original certification supplies both sides of the reply. Display ACKs
        # and today's sender aliases cannot admit a different incarnation.
        replies = {
            InputDispositions.bus_key(original.message, thread): original.message
            for original in self.bus.log.addressed_sources(lookup)
            if original.direct_for(lookup) and any(target.sent(original) for target in targets)
        }
        document = dispositions.read(tuple(replies))
        for key, message in replies.items():
            row = document.lookup(key)
            if not row.exists:
                # Read-only inspection of a canonical input. Only an
                # explicit successful review persists this observation.
                row = ReservedInput(
                    key,
                    message.seq,
                    thread.name,
                    snapshot.admission_generations[thread.name],
                    message.target,
                    message.body,
                )
            if row.owner in owners and row.unresolved:
                unknown[key] = row
                eligible.add(key)
        return GoalInputReview(
            goal_id,
            targets,
            owners,
            tuple(sorted(unknown.values(), key=lambda row: row.order)),
            frozenset(eligible),
        )

    def goal_history(
        self, name: str, *, goal_id: str | None = None
    ) -> tuple[GoalHistoryEntry, ...]:
        """Read durable revisions for one thread incarnation and optional goal ID."""
        with _store_lock(self._wire_lock_path, shared=True):
            return self.registry.goal_history(name, goal_id=goal_id)

    def goal_wait(self, name: str) -> GoalWait | None:
        waits = self.waits
        return waits.for_goal(self.registry.require(name).goal, waits.read())

    def recover_closed_goal_wait(self, name: str) -> tuple[str, ...]:
        """Release one stranded standby without replaying a dependency input.

        This is an owner-side liveness check, not an agent turn. A subsequent
        scheduler pass may continue the still-active goal only if its private
        attempt ledger has a fresh READY grant.
        """
        with self.writing() as completed:
            snapshot = self.registry.snapshot()
            canonical = snapshot.canonical_name(name)
            owner = snapshot.threads.get(canonical)
            if owner is None or owner.executing:
                return ()
            goal = owner.active_goal
            if goal is None:
                return ()
            waits = self.waits
            rows = waits.read()
            wait = rows.get(goal.id)
            if (
                wait is None
                or not wait.current_for(goal, owner.created_at)
            ):
                return ()
            closed = GoalWaits.closed_wait_group(canonical, wait.targets, rows, snapshot)
            if not closed:
                return ()
            # An unreadable bus raises to the drain loop, which records it as
            # the owner's visible drain diagnostic; it never proves silence.
            if wait.has_reply(self.bus.log):
                return ()
            names = ", ".join(f"@{member}" for member in closed)
            note = (
                f"Dependency wait group ({names}) has no independent worker. "
                "Standby was released; inspect dependencies and continue useful work."
            )
            progress = goal.progress.noted(note)
            self.write_goal(owner, replace(goal, progress=progress, revision=goal.revision + 1),
                            completed)
            if not waits.clear(goal.id, wait_id=wait.wait_id):
                return ()
            return closed

    def release_waits_after_terminal_turn(self, fence: FinishedTurnFence | None) -> tuple[str, ...]:
        """Release waits after the latest exact, still-idle child turn.

        ACP invokes this after terminal publication, never at the earlier UI
        `settled` event. A later turn (even already finished) invalidates the
        old fence; a rename or unrelated metadata edit does not.
        """
        if type(fence) is not FinishedTurnFence:
            return ()
        with self.writing() as completed:
            snapshot = self.registry.snapshot()
            canonical = snapshot.canonical_name(fence.identity.incarnation.name)
            source = snapshot.threads.get(canonical)
            observed = (
                source.observed_turn(snapshot.admission_generations.get(canonical, 0))
                if source is not None
                else None
            )
            if (
                source is None
                or source.executing
                or observed is None
                or not observed.matches(fence.renamed(canonical))
                or not snapshot.statuses[canonical].active
            ):
                return ()
            waits = self.waits.read()
            released: list[str] = []
            for owner in snapshot.threads.values():
                goal = owner.active_goal
                if goal is None:
                    continue
                wait = waits.get(goal.id)
                if (
                    wait is None
                    or not wait.current_for(goal, owner.created_at)
                    or not any(
                        snapshot.canonical_name(target.name) == canonical
                        and target.created_at == fence.identity.incarnation.created_at
                        and (generation := wait.target_turn_generations[index]) is not None
                        and 0 < generation <= fence.identity.generation
                        for index, target in enumerate(wait.targets)
                    )
                    or any(
                        GoalWaits.target_has_active_turn(target, snapshot)
                        for target in wait.targets
                    )
                ):
                    continue
                try:
                    reply = wait.has_reply(self.bus.log)
                except (OSError, ValueError, sqlite3.DatabaseError):
                    # The terminal turn has already committed. An unavailable
                    # optional reply read cannot prove silence or release this
                    # owner; do not turn the completed ACP turn into a failure.
                    # Registry/goal writes below remain outside this guard.
                    continue
                if reply:
                    continue
                diagnostic = (
                    f"Declared dependency @{canonical} finished without a qualifying direct "
                    "reply, and no declared dependency has an active turn. "
                    "Standby was released; inspect messages and UNKNOWN inputs, then "
                    "continue independent work or redelegate. No input was replayed."
                )
                progress = goal.progress.noted(diagnostic)
                # Persist the explanation before clearing the wait. A crash
                # between these writes leaves the active goal in standby;
                # the periodic closed-wait check can release it later.
                continued_goal = replace(goal, progress=progress, revision=goal.revision + 1)
                self.write_goal(owner, continued_goal, completed)
                self.waits.clear(goal.id, wait_id=wait.wait_id)
                released.append(owner.name)
            return tuple(released)

    def goal_execution(self, name: str) -> GoalExecution | None:
        return self._goal_snapshot(name)[1]

    def goal_changed(
        self, name: str, previous: tuple[Goal | None, GoalExecution | None] | None
    ) -> GoalChanged | None:
        """Announce a durable change through S1's event vocabulary.

        Existing registry/history and wait persistence are the cross-process
        authority. Each observer supplies its last successfully published view.
        """
        from .agent_events import GoalChanged

        current = self.goal_snapshot(name)
        return GoalChanged(*current) if current != previous else None

    def goal_snapshot(self, name: str) -> tuple[Goal | None, GoalExecution | None]:
        """Read current goal and its scheduling projection as one owner snapshot."""
        with _store_lock(self._wire_lock_path, shared=True):
            return self._goal_snapshot(name)

    def _goal_snapshot(self, name: str) -> tuple[Goal | None, GoalExecution | None]:
        snapshot = self.registry.snapshot()
        canonical = snapshot.canonical_name(name)
        goal = snapshot.threads[canonical].goal
        return goal, GoalWaits.execution(
            goal,
            self.waits.read(),
            snapshot,
        )

    def consume_goal_wait(self, name: str, wait_id: str) -> bool:
        """Called under the send-boundary wire lock after reserving an attempt."""
        goal = self.registry.require(name).active_goal
        return bool(
            goal is not None and self.waits.clear(goal.id, wait_id=wait_id)
        )

    def consume_reply_wait(self, owner: RegistryOwner, reference: MessageReference) -> bool:
        """A completed native input consumes only its original dependency wait.

        Selected handling calls this after corroborated completion, never after
        fetching, triage preparation or a failed/UNKNOWN native send.
        """
        with _store_lock(self._wire_lock_path):
            owner.require_registry(self.registry)
            current = self.registry.require(owner.thread.name)
            goal = current.continuation_goal(owner.thread)
            if goal is None:
                return False
            wait = self.waits.for_goal(goal, self.waits.read())
            if wait is None or wait.owner_created_at != current.created_at:
                return False
            with self.bus.log.certified_read() as source:
                original = source.delivery(reference.seq)
                if original.message.reference != reference:
                    raise ValueError("Dependency reply source reference changed.")
                if not wait.matches(original):
                    return False
            return self.waits.clear(goal.id, wait_id=wait.wait_id)

    def update_goal(
        self,
        name: str,
        action: GoalAction,
        *,
        actor: type = RuntimeInvocable,
    ) -> Goal | None:
        """Apply one declared command; external ingress owns decoding."""
        return action.apply(self, name, actor=actor)

    def block_goal_after_failed_turn(
        self,
        name: str,
        *,
        started_goal: Goal,
        expected_worktree: str,
        diagnostic: str,
    ) -> Goal | None:
        """Make an unresolved same-ID attempt visible without losing newer progress."""
        with self.writing() as completed:
            thread = self.registry.require(name)
            current = thread.goal_for(started_goal.id)
            if thread.worktree != expected_worktree or current is None:
                return thread.goal
            blocked = current.after_failed_turn(diagnostic)
            if blocked is current:
                return current
            return self.write_goal(thread, blocked, completed)

    def block_unverified_goal_completion(
        self,
        name: str,
        *,
        expected_goal: Goal,
        expected_worktree: str,
        diagnostic: str,
    ) -> Goal | None:
        """Revoke a provisional completion after the provider turn failed."""
        with self.writing() as completed:
            thread = self.registry.require(name)
            current = thread.goal
            if thread.worktree != expected_worktree or current is None or current != expected_goal:
                return current
            blocked = current.after_unverified_completion(diagnostic)
            if blocked is current:
                return current
            return self.write_goal(thread, blocked, completed)
