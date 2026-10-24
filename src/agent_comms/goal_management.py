"""Goal state, input reviews and wait lifecycle operations."""

from __future__ import annotations

from agent_comms.field_codec import GoalId
from .errors import StaleAttemptError
import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING

from .agent_activity import AgentActivity
from .coordination_errors import StaleFence
from .errors import RelationViolationError, UnregisteredThreadError
from .goal_actions import (
    GoalAction,
    GoalActionContext,
    GoalPrecondition,
    RuntimeInvocable,
    SentBackOrderGoalAction,
    SetGoalAction,
)
from .goal_attempts import Generation, GoalAttemptStore
from .errors import UnresolvedAttemptError
from .goal_history import GoalHistoryEntry
from .goal_waits import (
    GoalInputReview,
    GoalWait,
    GoalWaitReview,
    GoalWaits,
    RecordedGoalWait,
    StandbyReview,
)
from .native_input_owner import GoalLaunchOwner
from .owner_requests import NewestOpenRequest, RequestDirective
from .registration import Registration
from .store_files import SharedLock

if TYPE_CHECKING:
    from .agent_events import GoalChange
    from .message_reference import MessageReference
    from .messages import Message
    from .native_input_owner import RegistryOwner
    from .registry_document import RegistrySnapshot
    from .thread_identity import ThreadIncarnation
from .goal_presentation import (
    GoalExecution,
    GoalExecutionSource,
    GoalPublicationSnapshot,
    GoalSnapshot,
    GoalWaitTarget,
    IdleGoalExecution,
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

    def fork_goal(self, parent: Thread, child: Thread) -> Goal:
        """Create a commander's child goal from its parent's active work."""
        text = ('\n'.join(goal.text for goal in parent.goal.active())
                or f'Coordinate assigned work for {parent.name}.')
        return SetGoalAction(text=text).create(GoalActionContext(self, child))

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

    def fire(self, unit: str, reason: StrikeReason, actor: ThreadIncarnation) -> tuple[Todo, ...]:
        owner = self.registry.require(unit).incarnation
        store: TodoStore = self.todos
        return store.fire(owner, actor, reason)

    def open_goal_store(self) -> GoalAttemptStore:
        if self.goal_store is None:
            private = GoalAttemptStore.directory_in(self.root)
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

    def goal_input_review(
        self, name: str, goal_id: GoalId, wait_for: Sequence[str]
    ) -> StandbyReview:
        """Project exact review eligibility for one current goal and dependency set."""
        review = self._goal_input_review(self.registry.require(name), goal_id, wait_for)
        return review.standby_review()

    def _goal_input_review(
        self, thread: Thread, goal_id: GoalId, wait_for: Sequence[str]
    ) -> GoalInputReview:
        captured = GoalWaitReview.capture(self, thread.incarnation.require_recorded())
        thread = captured.owner
        snapshot = captured.snapshot
        goal = thread.goal.require().require_id(goal_id).require_active()
        if not wait_for:
            raise ValueError("Standby requires explicit wait_for thread names.")
        resolved = tuple(snapshot.require(target.removeprefix("@")) for target in wait_for)
        if any(target.incarnation == thread.incarnation for target in resolved):
            raise ValueError("A goal cannot wait for its own thread.")
        targets = tuple(
            dict.fromkeys(GoalWaitTarget(target.incarnation.require_recorded()) for target in resolved)
        )
        owners = captured.owners
        dispositions = InputDispositions(self.root)
        unknown = {row.key: row for row in dispositions.unresolved(owners).unknown(owners)}
        eligible = set()
        incarnation = thread.incarnation.require_recorded()
        lookup = incarnation.lookup
        after_seq = captured.wait.review_after_seq(
            goal, incarnation, targets
        )
        sequences = tuple({
            sequence for row in unknown.values()
            for sequence in row.bus_sequences_through(after_seq)
        })
        senders = tuple(target.incarnation.lookup for target in targets)
        replies: dict[str, Message] = {}
        with self.bus.bus.reading() as source:
            through_seq = source.revision()
            # Preserve older UNKNOWN rows; new inputs begin at this wait's own cursor.
            # Both selections use the same frozen recipient and sender projections.
            page = source.direct_replies(
                lookup, sequences=sequences, limit=len(sequences), descending=False,
                sender_lookups=senders,
            ) if sequences else ()
            while True:
                replies.update({
                    InputDispositions.bus_key(original.message, thread): original.message
                    for original in page
                })
                page = source.direct_replies(
                    lookup, after=after_seq, limit=100, descending=False,
                    sender_lookups=senders,
                )
                if not page:
                    break
                after_seq = page[-1].seq
        # Pure document inspection; only explicit successful review persists it.
        document = dispositions.read(tuple(replies)).record(*(
            ReservedInput(
                key, message.seq, thread.name, snapshot.claim_generation(thread.name),
                message.target, message.body,
            )
            for key, message in replies.items()
        ))
        for pending in document.unknown(owners):
            unknown[pending.key] = pending
            eligible.add(pending.key)
        return GoalInputReview(
            captured.snapshot,
            thread,
            captured.wait,
            captured.turn,
            through_seq,
            targets,
            tuple(sorted(unknown.values(), key=lambda row: row.order)),
            frozenset(eligible),
        )

    def goal_history(
        self, name: str, *, goal_id: GoalId | None = None
    ) -> tuple[GoalHistoryEntry, ...]:
        """Read durable revisions for one thread incarnation and optional goal ID."""
        return self.registry.goal_history(name, goal_id=goal_id)

    def goal_wait(self, name: str) -> GoalWait:
        goal = self.registry.require(name).goal
        return self.waits.resolve(goal)

    def recover_closed_goal_wait(self, name: str) -> tuple[str, ...]:
        """Read silence outside the registry, then release only the observed wait."""
        try:
            incarnation = self.registry.require(name).incarnation.require_recorded()
            review = GoalWaitReview.capture(self, incarnation)
        except UnregisteredThreadError:
            return ()
        owner = review.owner
        snapshot = review.snapshot
        if self.agents.is_working(owner.incarnation, snapshot) or not any(
            review.wait.current_for(goal, owner.incarnation.require_recorded())
            for goal in owner.goal.active()
        ):
            return ()
        closed = GoalWaits.closed_wait_group(
            owner.name, review.wait.targets, self.waits.read(), snapshot, self.agents,
        )
        if not closed:
            return ()
        # A failed read is a diagnostic, never evidence of silence.
        with review.publishing_unanswered(self) as unchanged:
            if not unchanged:
                return ()
            snapshot = self.registry.snapshot()
            owner, = snapshot.threads_for(review.owner.incarnation.require_recorded())
            if self.agents.is_working(owner.incarnation, snapshot):
                return ()
            closed = GoalWaits.closed_wait_group(
                owner.name, review.wait.targets, self.waits.read(), snapshot, self.agents,
            )
            if not closed:
                return ()
            names = ", ".join(f"@{member}" for member in closed)
            note = (
                f"Dependency wait group ({names}) has no independent worker. "
                "Standby was released; inspect dependencies and continue useful work."
            )
            return closed if self._release_reviewed_wait(review, note) else ()

    def release_waits_after_terminal_turn(self, fence: TurnCompletion) -> tuple[str, ...]:
        """A completed turn may release silence observed outside the registry lock."""
        snapshot = self.registry.snapshot()
        sources = fence.idle_sources(snapshot)
        if not sources:
            return ()
        source, = sources
        released: list[str] = []
        for wait in self.waits.targeting(source.incarnation.require_recorded()):
            for owner in snapshot.threads_for(wait.owner):
                try:
                    review = GoalWaitReview.capture(self, owner.incarnation.require_recorded())
                except UnregisteredThreadError:
                    continue
                if review.wait == wait:
                    released.extend(self._release_finished_wait(review, fence))
        return tuple(released)

    def _release_finished_wait(
        self, review: GoalWaitReview, fence: TurnCompletion,
    ) -> tuple[str, ...]:
        wait, owner = review.wait, review.owner
        if not any(wait.current_for(goal, owner.incarnation.require_recorded())
                   for goal in owner.goal.active()):
            return ()
        if any(self.agents.is_working(target.incarnation, review.snapshot)
               for target in wait.targets):
            return ()
        with review.publishing_unanswered(self) as unchanged:
            if not unchanged:
                return ()
            current = self.registry.snapshot()
            sources = fence.idle_sources(current)
            if not sources or any(self.agents.is_working(target.incarnation, current)
                                  for target in wait.targets):
                return ()
            actual_source, = sources
            note = (
                f"Declared dependency @{actual_source.name} finished without a qualifying direct "
                "reply, and no declared dependency has an active turn. "
                "Standby was released; inspect messages and UNKNOWN inputs, then "
                "continue independent work or redelegate. No input was replayed."
            )
            if self._release_reviewed_wait(review, note):
                actual_owner, = current.threads_for(owner.incarnation.require_recorded())
                return (actual_owner.name,)
        return ()

    def _release_reviewed_wait(self, review: GoalWaitReview, note: str) -> bool:
        """Publish a validated silent wait while its review owns the registry lock."""
        snapshot = self.registry.snapshot()
        owner, = snapshot.threads_for(review.owner.incarnation.require_recorded())
        goal = owner.goal.require_active()
        progress = f"{goal.progress}\n\n{note}" if goal.progress else note
        self.registry.register(
            replace(owner, goal=replace(goal, progress=progress, revision=goal.revision + 1)),
            snapshot.status(owner.name),
        )
        return review.wait.clear(self.waits)

    def goal_execution(self, name: str) -> GoalExecution:
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
        return self.snapshot_for(snapshot.threads[canonical], snapshot, self.waits.read())

    def snapshot_for(self, owner: Thread, snapshot: RegistrySnapshot,
                     waits: dict[str, RecordedGoalWait]) -> GoalSnapshot:
        """All goal readers build the same source from the original records."""
        for goal in owner.goal:
            store = self.open_goal_store()
            generation = store.require_generation(goal.id)
            source = GoalExecutionSource(
                goal, GoalWaits.for_goal(goal, waits), generation,
                generation.lifecycle.evidence(store, generation), owner, snapshot, self.agents,
            )
            return GoalSnapshot(goal, source.execution())
        return GoalSnapshot(owner.goal, IdleGoalExecution())

    def consume_goal_wait(self, name: str, wait_id: str) -> bool:
        """Consume the exact wait after reserving its original attempt."""
        goal = self.registry.require(name).goal.active()
        return goal.clear_wait(self.waits, wait_id=wait_id)

    def consume_reply_wait(self, owner: RegistryOwner, reference: MessageReference) -> bool:
        """Read the exact completed delivery before checking its original owner fence."""
        with self.registry.store.locked(mode=SharedLock()):
            owner.require_registry(self.registry)
            review = GoalWaitReview.capture(self, owner.thread.incarnation.require_recorded())
        incarnation = review.owner.incarnation.require_recorded()
        if not review.wait.owner_matches(incarnation):
            return False
        original, = self.bus.bus.deliveries_for_references((reference,))
        if not review.wait.matches(original, incarnation):
            return False
        with self.registry.store.locked():
            owner.require_registry(self.registry)
            if not review.current_for(self):
                return False
            current, = self.registry.snapshot().threads_for(incarnation)
            goal = current.continuation_goal(owner.thread)
            return any(review.wait.current_for(actual, incarnation)
                       and review.wait.clear(self.waits) for actual in goal)

    def direct(self, name: str, message: Message) -> None:
        """A plain owner message directs the recipient's newest open request."""
        owner = self.registry.require(name).incarnation
        directive = RequestDirective.from_message(message)
        for request in NewestOpenRequest().select(self.todos, owner):
            self.todos.record_directive(request.id, directive)

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
