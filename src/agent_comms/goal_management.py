"""Goal state, input reviews and wait lifecycle operations."""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from functools import cached_property
from itertools import chain
from pathlib import Path
from typing import TYPE_CHECKING

from .agent_activity import AgentActivity
from .coordination_errors import StaleFence
from .errors import RelationViolationError
from .goal_actions import (
    BlockedGoalAction,
    GoalAction,
    GoalActionContext,
    GoalPrecondition,
    RuntimeInvocable,
    SentBackOrderGoalAction,
)
from .goal_attempts import Generation, GoalAttemptStore, StaleAttemptError, UnresolvedAttemptError
from .goal_history import GoalHistoryEntry
from .goal_waits import (
    GoalInputReview,
    GoalWait,
    GoalWaits,
    NoGoalWait,
    RecordedGoalWait,
    StandbyReview,
)
from .native_input_owner import GoalLaunchOwner
from .registration import Registration

if TYPE_CHECKING:
    from .agent_events import GoalChange
    from .message_reference import MessageReference
    from .native_input_owner import RegistryOwner
    from .registry_document import RegistrySnapshot
from .goal_presentation import (
    GoalPublicationSnapshot,
    GoalSnapshot,
    GoalWaitTarget,
    ThreadGoalExecution,
)
from .goals import Goal, GoalDeclaration
from .input_attempt import (
    UNOBSERVED_DELIVERY_SCOPE,
    InputDeliveryScope,
    ReservedInput,
    UnresolvedInput,
)
from .input_disposition import DeliveryOverview, InputDispositions
from .message_bus import MessageBus
from .threads import Thread
from .todos import SendBackVerdict, StrikeReason, Todo, TodoCommand, TodoStore
from .turn_lease import TurnCompletion

_LOG = logging.getLogger(__name__)


class Goals:
    def __init__(self, root: Path, registry: Registration, bus: MessageBus, agents: AgentActivity):
        self.root = root
        self.registry = registry
        self.bus = bus
        self.agents = agents
        self.waits = GoalWaits.at(root)
        self.goal_store: GoalAttemptStore | None = None

    @cached_property
    def todos(self) -> TodoStore:
        return TodoStore(self.root)

    def change_problem(self, problem, operation, commander):
        actor = self.registry.require(commander).incarnation
        current = self.todos.get(problem)
        current.require_commander(actor)
        return operation.prepare(self, current).record(self, problem)

    def apply_problem(self, problem: str, operation: TodoCommand) -> Todo:
        return self.todos.apply(problem, operation)

    def send_back(self, problem: str, verdict: SendBackVerdict) -> Todo:
        """The verdict and strike land on the Todo in the write that revises the holder's Goal."""
        holder = verdict.previous
        self.update_goal(holder.owner, SentBackOrderGoalAction(
            expect=GoalPrecondition(goal_id=holder.generation), problem=problem, operation=verdict))
        sent_back: Todo = self.todos.get(problem)
        return sent_back

    def fire(self, unit: str, reason: StrikeReason, commander: str) -> tuple[Todo, ...]:
        actor = self.registry.require(commander).incarnation
        store: TodoStore = self.todos
        return store.fire(unit, actor, reason)

    def open_goal_store(self) -> GoalAttemptStore:
        if self.goal_store is None:
            private = self.root / GoalAttemptStore.root_directory
            private.mkdir(mode=0o700, exist_ok=True)
            self.goal_store = GoalAttemptStore.initialize(private)
        return self.goal_store

    def ready_goal_grant_locked(
        self, owner: Thread, claim_scope: int, generation: Generation
    ) -> str:
        """Caller holds registry; recover only this owner's original unused READY grant."""
        try:
            goal = GoalLaunchOwner(thread=owner, claimed_generation=claim_scope).require_ready(
                self.registry.snapshot(), generation.goal_id,
            )
            goal.order.require_order(goal, self, owner.incarnation)
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
        scope: InputDeliveryScope = UNOBSERVED_DELIVERY_SCOPE,
    ) -> DeliveryOverview:
        """Project durable notices using current owner queue facts when available."""
        aliases = self.registry.entry(name).entry.names
        return (
            InputDispositions(self.root)
            .unresolved(aliases)
            .delivery_overview(
                aliases, include_history=include_history, scope=scope
            )
        )

    def dismiss_historical_inputs(
        self, name: str, *, scope: InputDeliveryScope = UNOBSERVED_DELIVERY_SCOPE
    ) -> DeliveryOverview:
        """Dismiss only historical notices; UNKNOWN remains unresolved and unreplayable."""
        aliases = self.registry.entry(name).entry.names
        return InputDispositions(self.root).dismiss_historical(
            aliases, scope=scope
        )

    def goal_input_review(self, name: str, goal_id: str, wait_for: Sequence[str]) -> StandbyReview:
        """Project exact review eligibility for one current goal and dependency set."""
        review = self._goal_input_review(self.registry.require(name), goal_id, wait_for)
        return review.standby_review()

    def _goal_input_review(
        self, thread: Thread, goal_id: str, wait_for: Sequence[str]
    ) -> GoalInputReview:
        goal = thread.goal.require().require_id(goal_id).require_active()
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
        with self.bus.bus.reading() as source:
            earlier = source.conversation(
                lookup, sequences=sequences, limit=len(sequences), descending=False,
            ) if sequences else ()
            # Retain older UNKNOWN inputs and inspect new frozen-recipient replies
            # from one cut. Do not truncate the eligible dependency suffix.
            fresh = source.received(
                lookup, after=after_seq, limit=source.revision(), descending=False,
                sender_lookups=tuple(target.incarnation.lookup for target in targets),
            )
            replies = {
                InputDispositions.bus_key(original.message, thread): original.message
                for original in chain(earlier, fresh)
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
                    snapshot.claim_generation(thread.name),
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
        return self.registry.goal_history(name, goal_id=goal_id)

    def goal_wait(self, name: str) -> GoalWait:
        goal = self.registry.require(name).goal
        return self.waits.resolve(goal)

    def recover_closed_goal_wait(self, name: str) -> tuple[str, ...]:
        """Release one stranded standby without replaying a dependency input.

        This is an owner-side liveness check, not an agent turn. A subsequent
        scheduler pass may continue the still-active goal only if its private
        attempt ledger has a fresh READY grant.
        """
        with self.registry.store.locked():
            snapshot = self.registry.snapshot()
            canonical = snapshot.canonical_name(name)
            owner = snapshot.threads.get(canonical)
            if owner is None or self.agents.is_working(owner.incarnation, snapshot):
                return ()
            return tuple(member for goal in owner.goal.active()
                         for member in self._recover_closed_wait(goal, owner, snapshot, canonical))

    def _recover_closed_wait(self, goal: Goal, owner: Thread, snapshot: RegistrySnapshot,
                             canonical: str) -> tuple[str, ...]:
        waits = self.waits
        rows = waits.read()
        wait = rows.get(goal.id)
        if wait is None or not wait.current_for(goal, owner.incarnation.require_recorded()):
            return ()
        closed = GoalWaits.closed_wait_group(canonical, wait.targets, rows, snapshot, self.agents)
        if not closed:
            return ()
        # An unreadable bus remains a drain diagnostic, never evidence of silence.
        if wait.has_reply(self.bus.bus, owner.incarnation.require_recorded()):
            return ()
        names = ", ".join(f"@{member}" for member in closed)
        note = (
            f"Dependency wait group ({names}) has no independent worker. "
            "Standby was released; inspect dependencies and continue useful work."
        )
        progress = f"{goal.progress}\n\n{note}" if goal.progress else note
        self.registry.register(
            replace(owner, goal=replace(goal, progress=progress, revision=goal.revision + 1)),
            snapshot.status(canonical),
        )
        if not waits.clear(goal.id, wait_id=wait.wait_id):
            return ()
        return closed

    def release_waits_after_terminal_turn(self, fence: TurnCompletion) -> tuple[str, ...]:
        """Release waits after the latest exact, still-idle child turn.

        ACP invokes this after terminal publication, never at the earlier UI
        `settled` event. A later turn (even already finished) invalidates the
        old fence; a rename or unrelated metadata edit does not.
        """
        with self.registry.store.locked():
            snapshot = self.registry.snapshot()
            sources = fence.idle_sources(snapshot)
            if not sources:
                return ()
            (source,) = sources
            canonical = source.name
            released: list[str] = []
            for wait in self.waits.targeting(source.incarnation.require_recorded()):
                for owner in snapshot.threads_for(wait.owner):
                    if any(
                        self._release_finished_wait(
                            goal,
                            owner,
                            snapshot,
                            canonical,
                            wait.goal_id,
                            wait,
                        )
                        for goal in owner.goal.active()
                    ):
                        released.append(owner.name)
            return tuple(released)

    def _release_finished_wait(
        self,
        goal: Goal,
        owner: Thread,
        snapshot: RegistrySnapshot,
        canonical: str,
        goal_id: str,
        wait: RecordedGoalWait,
    ) -> bool:
        if goal.id != goal_id or not wait.current_for(goal, owner.incarnation.require_recorded()):
            return False
        if any(self.agents.is_working(target.incarnation, snapshot) for target in wait.targets):
            return False
        if wait.has_reply(self.bus.bus, owner.incarnation.require_recorded()):
            return False
        diagnostic = (
            f"Declared dependency @{canonical} finished without a qualifying direct "
            "reply, and no declared dependency has an active turn. "
            "Standby was released; inspect messages and UNKNOWN inputs, then "
            "continue independent work or redelegate. No input was replayed."
        )
        progress = f"{goal.progress}\n\n{diagnostic}" if goal.progress else diagnostic
        # Persist the explanation before releasing the exact wait.
        continued_goal = replace(goal, progress=progress, revision=goal.revision + 1)
        self.registry.register(replace(owner, goal=continued_goal), snapshot.status(owner.name))
        self.waits.clear(goal.id, wait_id=wait.wait_id)
        return True

    def goal_execution(self, name: str) -> ThreadGoalExecution:
        return self._goal_snapshot(name).execution

    def goal_changed(self, name: str, previous: GoalPublicationSnapshot) -> GoalChange:
        """Announce a durable change through S1's event vocabulary.

        Existing registry/history and wait persistence are the cross-process
        authority. Each observer supplies its last successfully published view.
        """
        from .agent_events import GoalChanged, GoalUnchanged

        current = self.goal_snapshot(name)
        return GoalChanged(current) if previous.changed_by(current) else GoalUnchanged()

    def goal_snapshot(self, name: str) -> GoalSnapshot:
        """Read current goal and its scheduling projection as one owner snapshot."""
        return self._goal_snapshot(name)

    def _goal_snapshot(self, name: str) -> GoalSnapshot:
        snapshot = self.registry.snapshot()
        canonical = snapshot.canonical_name(name)
        goal = snapshot.threads[canonical].goal
        execution = GoalWaits.execution(goal, self.waits.read(), snapshot, self.agents)
        return GoalSnapshot(goal, execution)

    def consume_goal_wait(self, name: str, wait_id: str) -> bool:
        """Consume the exact wait after reserving its original attempt."""
        goal = self.registry.require(name).goal.active()
        return goal.clear_wait(self.waits, wait_id=wait_id)

    def consume_reply_wait(self, owner: RegistryOwner, reference: MessageReference) -> bool:
        """A completed native input consumes only its original dependency wait.

        Selected handling calls this after corroborated completion, never after
        fetching, triage preparation or a failed/UNKNOWN native send.
        """
        with self.registry.store.locked():
            owner.require_registry(self.registry)
            current = self.registry.require(owner.thread.name)
            goal = current.continuation_goal(owner.thread)
            return any(self._consume_reply_wait(actual, current, reference) for actual in goal)

    def _consume_reply_wait(self, goal: Goal, current: Thread, reference: MessageReference) -> bool:
        wait = self.waits.for_goal(goal, self.waits.read())
        if not wait.owner_matches(current.incarnation.require_recorded()):
            return False
        original, = self.bus.bus.deliveries_for_references((reference,))
        if not wait.matches(original, current.incarnation.require_recorded()):
            return False
        return wait.consume_reply(self.waits, goal.id)

    def update_goal[Result: GoalDeclaration](
        self,
        name: str,
        action: GoalAction[Result],
        *,
        actor: type = RuntimeInvocable,
    ) -> Result:
        """Return the exact declaration written by this action."""
        with self._updating_goal(name, action, actor=actor) as goal:
            return goal

    def update_goal_snapshot[Result: GoalDeclaration](
        self, name: str, action: GoalAction[Result], *, actor: type,
    ) -> GoalSnapshot:
        """Return this publication and its schedule before another owner can change it."""
        with self._updating_goal(name, action, actor=actor):
            return self._goal_snapshot(name)

    @contextmanager
    def _updating_goal[Result: GoalDeclaration](
        self, name: str, action: GoalAction[Result], *, actor: type,
    ) -> Iterator[Result]:
        """Run checks before locking, then recheck the original goal at publication."""
        action.require_actor(actor)
        action = action.prepare(self, name, actor)
        with self.registry.store.locked():
            yield action.apply(GoalActionContext(self, self.registry.require(name), actor))

    def block_failed_origin(self, name: str, goal: Goal) -> GoalDeclaration:
        return self.update_goal(name, BlockedGoalAction(
            expect=GoalPrecondition(goal_id=goal.id, expected_goal=goal),
            progress="Goal origin turn did not finish successfully.",
        ))

    def block_goal_after_failed_turn(
        self,
        name: str,
        *,
        started_goal: Goal,
        expected_worktree: str,
        diagnostic: str,
    ) -> GoalDeclaration:
        """Make an unresolved same-ID attempt visible without losing newer progress."""
        with self.registry.store.locked():
            thread = self.registry.require(name)
            current = thread.goal.matching(started_goal.id)
            if thread.worktree != expected_worktree:
                return thread.goal
            blocked = current.after_failed_turn(diagnostic)
            if blocked is current:
                return thread.goal
            self.registry.register(replace(thread, goal=blocked), self.registry.status(thread.name))
            return blocked
