"""Goal state, input reviews and wait lifecycle operations."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .goal_actions import GoalAction, GoalActionContext, RuntimeInvocable
from .goal_attempts import Generation, GoalAttemptStore, StaleAttemptError, UnresolvedAttemptError
from .coordination_errors import StaleFence
from .errors import RelationViolationError
from .native_input_owner import GoalLaunchOwner
from .goal_history import GoalHistoryEntry
from .goal_waits import GoalInputReview, GoalWait, GoalWaits, NoGoalWait
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
    def __init__(self, root: Path, registry: Registration, bus: MessageBus):
        self.root = root
        self.registry = registry
        self.bus = bus
        self._wire_lock_path = root / "wire"
        self.waits = GoalWaits(root / GoalWaits.filename)
        self.goal_store: GoalAttemptStore | None = None

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
        if any(target.incarnation == thread.incarnation for target in resolved):
            raise ValueError("A goal cannot wait for its own thread.")
        targets = tuple(
            dict.fromkeys(GoalWaitTarget(target.incarnation.require_recorded()) for target in resolved)
        )
        owners = self.registry.aliases_for(thread.name)
        dispositions = InputDispositions(self.root)
        unknown = {row.key: row for row in dispositions.unresolved(owners).unknown(owners)}
        eligible = set()
        snapshot = self.registry.snapshot()
        from .input_attempt import ReservedInput

        lookup = thread.incarnation.lookup
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

    def goal_wait(self, name: str) -> GoalWait:
        goal = self.registry.require(name).goal
        return self.waits.for_goal(goal, self.waits.read()) if goal is not None else NoGoalWait()

    def recover_closed_goal_wait(self, name: str) -> tuple[str, ...]:
        """Release one stranded standby without replaying a dependency input.

        This is an owner-side liveness check, not an agent turn. A subsequent
        scheduler pass may continue the still-active goal only if its private
        attempt ledger has a fresh READY grant.
        """
        with _store_lock(self._wire_lock_path):
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
            if wait.has_reply(self.bus.log, owner.incarnation.require_recorded()):
                return ()
            names = ", ".join(f"@{member}" for member in closed)
            note = (
                f"Dependency wait group ({names}) has no independent worker. "
                "Standby was released; inspect dependencies and continue useful work."
            )
            progress = f"{goal.progress}\n\n{note}" if goal.progress else note
            self.registry.register(
                replace(owner, goal=replace(goal, progress=progress, revision=goal.revision + 1)),
                snapshot.statuses[canonical],
            )
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
        with _store_lock(self._wire_lock_path):
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
                        target.incarnation.resolved(snapshot)
                        == fence.identity.incarnation.resolved(snapshot)
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
                    reply = wait.has_reply(self.bus.log, owner.incarnation.require_recorded())
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
                progress = f"{goal.progress}\n\n{diagnostic}" if goal.progress else diagnostic
                # Persist the explanation before clearing the wait. A crash
                # between these writes leaves the active goal in standby;
                # the periodic closed-wait check can release it later.
                continued_goal = replace(goal, progress=progress, revision=goal.revision + 1)
                self.registry.register(
                    replace(owner, goal=continued_goal), snapshot.statuses[owner.name]
                )
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
        execution = (
            GoalWaits.for_goal(goal, self.waits.read()).project(goal, snapshot)
            if goal is not None else None
        )
        return goal, execution

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
            if not wait.owner_matches(current.created_at):
                return False
            with self.bus.log.certified_read() as source:
                original = source.delivery(reference.seq)
                if original.message.reference != reference:
                    raise ValueError("Dependency reply source reference changed.")
                if not wait.matches(original, current.incarnation.require_recorded()):
                    return False
            return wait.consume_reply(self.waits, goal.id)

    def update_goal(
        self,
        name: str,
        action: GoalAction,
        *,
        actor: type = RuntimeInvocable,
    ) -> Goal | None:
        """Apply one declared command; external ingress owns decoding."""
        with _store_lock(self._wire_lock_path):
            return action.apply(
                GoalActionContext(self, self.registry.require(name), actor)
            )

    def block_goal_after_failed_turn(
        self,
        name: str,
        *,
        started_goal: Goal,
        expected_worktree: str,
        diagnostic: str,
    ) -> Goal | None:
        """Make an unresolved same-ID attempt visible without losing newer progress."""
        with _store_lock(self._wire_lock_path):
            thread = self.registry.require(name)
            current = thread.goal_for(started_goal.id)
            if thread.worktree != expected_worktree or current is None:
                return thread.goal
            blocked = current.after_failed_turn(diagnostic)
            if blocked is current:
                return current
            self.registry.register(replace(thread, goal=blocked), self.registry.status(thread.name))
            return blocked

    def block_unverified_goal_completion(
        self,
        name: str,
        *,
        expected_goal: Goal,
        expected_worktree: str,
        diagnostic: str,
    ) -> Goal | None:
        """Revoke a provisional completion after the provider turn failed."""
        with _store_lock(self._wire_lock_path):
            thread = self.registry.require(name)
            current = thread.goal
            if thread.worktree != expected_worktree or current is None or current != expected_goal:
                return current
            blocked = current.after_unverified_completion(diagnostic)
            if blocked is current:
                return current
            self.registry.register(replace(thread, goal=blocked), self.registry.status(thread.name))
            return blocked
