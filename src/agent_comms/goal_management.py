"""Goal state, input reviews and wait lifecycle operations."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from functools import cached_property
from itertools import chain
from pathlib import Path
from typing import TYPE_CHECKING

from .goal_actions import (GoalAction, GoalActionContext, GoalPrecondition, RuntimeInvocable,
                           SentBackOrderGoalAction)
from .goal_attempts import Generation, GoalAttemptStore, StaleAttemptError, UnresolvedAttemptError
from .coordination_errors import StaleFence
from .errors import RelationViolationError
from .native_input_owner import GoalLaunchOwner
from .goal_history import GoalHistoryEntry
from .goal_waits import GoalInputReview, GoalWait, GoalWaits, NoGoalWait, StandbyReview
from .registration import Registration
from .agent_activity import AgentActivity

if TYPE_CHECKING:
    from .agent_events import GoalChanged
    from .message_reference import MessageReference
    from .native_input_owner import RegistryOwner
from .goal_presentation import GoalSnapshot, ThreadGoalExecution, GoalWaitTarget
from .goals import Goal
from .message_bus import MessageBus
from .store_files import SharedLock, _store_lock
from .threads import Thread
from .input_attempt import ReservedInput, UnresolvedInput
from .input_disposition import DeliveryOverview, InputDispositions
from .turn_lease import FinishedTurnFence
from .todos import SendBackVerdict, StrikeReason, Todo, TodoStore
from .wire_log import ConversationSourceWindowRequest

_LOG = logging.getLogger(__name__)


class Goals:
    def __init__(self, root: Path, registry: Registration, bus: MessageBus, agents: AgentActivity):
        self.root = root
        self.registry = registry
        self.bus = bus
        self.agents = agents
        self._wire_lock_path = root / "wire"
        self.waits = GoalWaits.at(root)
        self.goal_store: GoalAttemptStore | None = None

    @cached_property
    def todos(self):
        return TodoStore(self.root)

    def change_problem(self, problem, operation, commander):
        actor = self.registry.require(commander).incarnation
        current = self.todos.get(problem)
        current.require_commander(actor)
        return operation.prepare(self, current).record(self, problem)

    def apply_problem(self, problem, operation):
        with _store_lock(self._wire_lock_path):
            return self.todos.apply(problem, operation)

    def send_back(self, problem: str, verdict: SendBackVerdict) -> Todo:
        """The send-back's verdict and strike land on the Todo in the same write that revises the holder's Goal."""
        self.update_goal(verdict.previous.owner, SentBackOrderGoalAction(
            expect=GoalPrecondition(goal_id=verdict.previous.generation), problem=problem, operation=verdict))
        return self.todos.get(problem)

    def fire(self, unit: str, reason: StrikeReason, commander: str) -> tuple[Todo, ...]:
        actor = self.registry.require(commander).incarnation
        store: TodoStore = self.todos
        with _store_lock(self._wire_lock_path):
            return store.fire(unit, actor, reason)

    def open_goal_store(self) -> GoalAttemptStore:
        if self.goal_store is None:
            private = self.root / "goal-private"
            private.mkdir(mode=0o700, exist_ok=True)
            self.goal_store = GoalAttemptStore.initialize(private)
        return self.goal_store

    def ready_goal_grant_locked(
        self, owner: Thread, claim_scope: int, generation: Generation
    ) -> str:
        """Caller holds wire; recover only this owner's original unused READY grant."""
        try:
            GoalLaunchOwner(thread=owner, claimed_generation=claim_scope).require_ready(
                self.registry.snapshot(), generation.goal_id,
            ).require_order(self, owner.incarnation)
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

    def unresolved_inputs(self, name: str) -> tuple[UnresolvedInput, ...]:
        """Project durable unresolved inputs; reading never schedules another attempt."""
        with _store_lock(self._wire_lock_path, mode=SharedLock()):
            rows = (
                InputDispositions(self.root)
                .unresolved(aliases := self.registry.entry(name).entry.names)
                .unknown(aliases)
            )
            return tuple(UnresolvedInput.of(row) for row in rows)

    def input_delivery(
        self,
        name: str,
        *,
        include_history: bool = False,
        awaiting_keys: frozenset[str] | None = None,
    ) -> DeliveryOverview:
        """Project durable notices using current owner queue facts when available."""
        with _store_lock(self._wire_lock_path, mode=SharedLock()):
            aliases = self.registry.entry(name).entry.names
            return (
                InputDispositions(self.root)
                .unresolved(aliases)
                .delivery_overview(
                    aliases, include_history=include_history, awaiting_keys=awaiting_keys
                )
            )

    def dismiss_historical_inputs(
        self, name: str, *, awaiting_keys: frozenset[str] | None = None
    ) -> DeliveryOverview:
        """Dismiss only historical notices; UNKNOWN remains unresolved and unreplayable."""
        with _store_lock(self._wire_lock_path):
            aliases = self.registry.entry(name).entry.names
            return InputDispositions(self.root).dismiss_historical(
                aliases, awaiting_keys=awaiting_keys
            )

    def goal_input_review(self, name: str, goal_id: str, wait_for: Sequence[str]) -> StandbyReview:
        """Project exact review eligibility for one current goal and dependency set."""
        with _store_lock(self._wire_lock_path, mode=SharedLock()):
            review = self._goal_input_review(self.registry.require(name), goal_id, wait_for)
            return review.standby_review()

    def _goal_input_review(
        self, thread: Thread, goal_id: str, wait_for: Sequence[str]
    ) -> GoalInputReview:
        goal = thread.require_active_goal(goal_id)
        if not wait_for:
            raise ValueError("Standby requires explicit wait_for thread names.")
        resolved = tuple(self.registry.require(target.removeprefix("@")) for target in wait_for)
        if any(target.incarnation == thread.incarnation for target in resolved):
            raise ValueError("A goal cannot wait for its own thread.")
        targets = tuple(
            dict.fromkeys(GoalWaitTarget(target.incarnation.require_recorded()) for target in resolved)
        )
        snapshot = self.registry.snapshot()
        owners = snapshot.names_for(thread.name)
        dispositions = InputDispositions(self.root)
        unknown = {row.key: row for row in dispositions.unresolved(owners).unknown(owners)}
        eligible = set()
        lookup = thread.incarnation.lookup
        after_seq = self.goal_wait(thread.name).review_after_seq(
            goal, thread.incarnation.require_recorded(), targets
        )
        sequences = tuple({
            sequence for row in unknown.values()
            for sequence in row.bus_sequences_through(after_seq)
        })
        request = ConversationSourceWindowRequest(
            sequences=sequences, limit=len(sequences), ascending=True,
        )
        earlier = request.capture_many(self.bus.log, lookup, (request,))[0] if sequences else ()
        # Keep older UNKNOWNs, but discover unreserved replies only after this
        # dependency scope's recorded wait. Certification owns both identities.
        replies = {
            InputDispositions.bus_key(original.message, thread): original.message
            for original in chain(
                earlier, self.bus.log.addressed_sources(lookup, after_seq=after_seq)
            )
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
                    snapshot.claimed_generations[thread.name],
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
        with _store_lock(self._wire_lock_path, mode=SharedLock()):
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
            if owner is None or self.agents.is_working(owner.incarnation, snapshot):
                return ()
            goal = owner.active_goal
            if goal is None:
                return ()
            waits = self.waits
            rows = waits.read()
            wait = rows.get(goal.id)
            if (
                wait is None
                or not wait.current_for(goal, owner.incarnation.require_recorded())
            ):
                return ()
            closed = GoalWaits.closed_wait_group(canonical, wait.targets, rows, snapshot, self.agents)
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
        if fence is None:
            return ()
        with _store_lock(self._wire_lock_path):
            snapshot = self.registry.snapshot()
            canonical = snapshot.canonical_name(fence.identity.incarnation.name)
            source = snapshot.threads.get(canonical)
            observed = (
                source.observed_turn(snapshot.claimed_generations.get(canonical, 0))
                if source is not None
                else None
            )
            if (
                source is None
                or source.turn_record.read().executing
                or observed is None
                or not observed.matches(fence.renamed(canonical))
                or not snapshot.statuses[canonical].active
            ):
                return ()
            waits = self.waits.read()
            released: list[str] = []
            for goal_id, wait in waits.items():
                if not any(
                    target.incarnation.resolved(snapshot) == source.incarnation
                    and (generation := wait.target_turn_generations[index]) is not None
                    and 0 < generation <= fence.identity.generation
                    for index, target in enumerate(wait.targets)
                ):
                    continue
                owner = snapshot.threads.get(snapshot.canonical_name(wait.owner.name))
                if owner is None:
                    continue
                goal = owner.active_goal
                if (
                    goal is None
                    or goal.id != goal_id
                    or not wait.current_for(goal, owner.incarnation.require_recorded())
                    or any(
                        self.agents.is_working(target.incarnation, snapshot)
                        for target in wait.targets
                    )
                ):
                    continue
                if wait.has_reply(self.bus.log, owner.incarnation.require_recorded()):
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

    def goal_execution(self, name: str) -> ThreadGoalExecution:
        return self._goal_snapshot(name).execution

    def goal_changed(self, name: str, previous: GoalSnapshot | None) -> GoalChanged | None:
        """Announce a durable change through S1's event vocabulary.

        Existing registry/history and wait persistence are the cross-process
        authority. Each observer supplies its last successfully published view.
        """
        from .agent_events import GoalChanged

        current = self.goal_snapshot(name)
        return GoalChanged(current) if current != previous else None

    def goal_snapshot(self, name: str) -> GoalSnapshot:
        """Read current goal and its scheduling projection as one owner snapshot."""
        with _store_lock(self._wire_lock_path, mode=SharedLock()):
            return self._goal_snapshot(name)

    def _goal_snapshot(self, name: str) -> GoalSnapshot:
        snapshot = self.registry.snapshot()
        canonical = snapshot.canonical_name(name)
        goal = snapshot.threads[canonical].goal
        execution = GoalWaits.execution(goal, self.waits.read(), snapshot, self.agents)
        return GoalSnapshot(goal, execution)

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
            if not wait.owner_matches(current.incarnation.require_recorded()):
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
        """Run checks before locking, then recheck the original goal at publication."""
        action.require_actor(actor)
        action = action.prepare(self, name, actor)
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
