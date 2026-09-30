"""Goal state, input reviews and wait lifecycle operations."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .goal_actions import GoalAction, GoalActionContext, RuntimeInvocable
from .goal_history import GoalHistoryEntry
from .goal_waits import GoalInputReview, GoalReplyScope, GoalWait, GoalWaits
from .registration import Registration

if TYPE_CHECKING:
    from .agent_events import GoalChanged
    from .goal_attempts import GoalAttemptStore
from .goal_presentation import GoalExecution, GoalWaitTarget
from .goals import Goal
from .message_bus import MessageBus
from .message_page import MessagePageRequest
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

    def unresolved_inputs(self, name: str) -> list[dict[str, Any]]:
        """Project durable unresolved inputs; reading never schedules another attempt."""
        from .input_disposition import InputDispositions

        with _store_lock(self._wire_lock_path):
            self.registry.require(name)
            rows = (
                InputDispositions(self.root / InputDispositions.filename)
                .read()
                .unknown(self.registry.aliases_for(name))
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

        with _store_lock(self._wire_lock_path):
            self.registry.require(name)
            aliases = self.registry.aliases_for(name)
            return (
                InputDispositions(self.root / InputDispositions.filename)
                .read()
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
            return InputDispositions(self.root / InputDispositions.filename).dismiss_historical(
                aliases, awaiting_keys=awaiting_keys
            )

    def goal_input_review(self, name: str, goal_id: str, wait_for: Sequence[str]) -> dict:
        """Project exact review eligibility for one current goal and dependency set."""
        with _store_lock(self._wire_lock_path):
            return self._goal_input_review(self.registry.require(name), goal_id, wait_for).public()

    def _goal_input_review(
        self, thread: Thread, goal_id: str, wait_for: Sequence[str]
    ) -> GoalInputReview:
        from .input_disposition import InputDispositions

        goal = thread.goal
        if goal is None or goal.id != goal_id:
            raise ValueError("This goal was replaced or cleared; refresh its state.")
        if not goal.state.active:
            raise ValueError(
                (pause.instruction() if (pause := goal.state.pause_source) else None)
                or "This goal is no longer active; refresh its state."
            )
        if not wait_for:
            raise ValueError("Standby requires explicit wait_for thread names.")
        resolved = tuple(self.registry.require(target.removeprefix("@")) for target in wait_for)
        if any(target.created_at == thread.created_at for target in resolved):
            raise ValueError("A goal cannot wait for its own thread.")
        targets = tuple(
            dict.fromkeys(GoalWaitTarget(target.name, target.created_at) for target in resolved)
        )
        owners = self.registry.aliases_for(thread.name)
        senders = frozenset(
            alias for target in resolved for alias in self.registry.aliases_for(target.name)
        )
        document = InputDispositions(self.root / InputDispositions.filename).read()
        unknown = {row.key: row for row in document.unknown(owners)}
        eligible = set()
        snapshot = self.registry.snapshot()
        delivery = self.bus._delivery_scope(thread.name, snapshot)
        from .input_attempt import ReservedInput

        # The canonical delivery scope excludes previous incarnations, while
        # viewer read ACKs deliberately have no bearing on native handling.
        with self.bus.log.locked():
            for item in self.bus._iter_delivery_messages_unlocked():
                message = item.message
                if not (
                    delivery.current(item, snapshot)
                    and message.target in owners
                    and message.sender in senders
                    and not message.notice
                    and message.membership is None
                ):
                    continue
                key = InputDispositions.bus_key(message, thread)
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
            senders,
            tuple(sorted(unknown.values(), key=lambda row: row.order)),
            frozenset(eligible),
        )

    def goal_history(
        self, name: str, *, goal_id: str | None = None
    ) -> tuple[GoalHistoryEntry, ...]:
        """Read durable revisions for one thread incarnation and optional goal ID."""
        with _store_lock(self._wire_lock_path):
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
        with _store_lock(self._wire_lock_path):
            snapshot = self.registry.snapshot()
            canonical = snapshot.aliases.get(name, name)
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
                or wait.owner_created_at != owner.created_at
                or wait.revision > goal.revision
            ):
                return ()
            closed = GoalWaits.closed_wait_group(canonical, wait.targets, rows, snapshot)
            if not closed:
                return ()
            try:
                reply = MessagePageRequest.capture(
                    GoalReplyScope(canonical, wait, snapshot), after=wait.after_seq, limit=1
                ).read(self.bus.log)
            except (OSError, ValueError, sqlite3.DatabaseError):
                # An unavailable read cannot prove that no reply was delivered.
                return ()
            if reply.messages:
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
            canonical = snapshot.aliases.get(
                fence.identity.incarnation.name, fence.identity.incarnation.name
            )
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
                    or wait.owner_created_at != owner.created_at
                    or wait.revision > goal.revision
                    or not any(
                        snapshot.aliases.get(target.name, target.name) == canonical
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
                    reply = MessagePageRequest.capture(
                        GoalReplyScope(owner.name, wait, snapshot), after=wait.after_seq, limit=1
                    ).read(self.bus.log)
                except (OSError, ValueError, sqlite3.DatabaseError):
                    # The terminal turn has already committed. An unavailable
                    # optional reply read cannot prove silence or release this
                    # owner; do not turn the completed ACP turn into a failure.
                    # Registry/goal writes below remain outside this guard.
                    continue
                if reply.messages:
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
        with _store_lock(self._wire_lock_path):
            return self._goal_snapshot(name)

    def _goal_snapshot(self, name: str) -> tuple[Goal | None, GoalExecution | None]:
        snapshot = self.registry.snapshot()
        canonical = snapshot.aliases.get(name, name)
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

    def update_goal(
        self,
        name: str,
        action: GoalAction,
        *,
        actor: type = RuntimeInvocable,
        owner_store: GoalAttemptStore | None = None,
    ) -> Goal | None:
        """Apply one declared command; external ingress owns decoding."""
        with _store_lock(self._wire_lock_path):
            return action.apply(
                GoalActionContext(self, self.registry.require(name), actor, owner_store)
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
            current = thread.goal
            if (
                thread.worktree != expected_worktree
                or current is None
                or current.id != started_goal.id
            ):
                return current
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
