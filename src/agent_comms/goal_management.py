"""Goal state, input reviews and wait lifecycle operations."""

from __future__ import annotations

from .agent_events import (
    GoalChanged,
    GoalUnchanged,
)

from .errors import StaleAttemptError
import logging
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from agent_comms.field_codec import GoalId
from .agent_activity import AgentActivity
from .coordination_errors import StaleFence
from .errors import RelationViolationError
from .goal_actions import (
    GoalAction,
    GoalActionContext,
    GoalPublicationContext,
    GoalPrecondition,
    RuntimeInvocable,
    SentBackOrderGoalAction,
    SetGoalAction,
)
from .goal_attempts import Generation, GoalAttemptFile, GoalAttemptStore
from .errors import UnresolvedAttemptError
from .goal_history import GoalHistoryEntry, RecordedGoalHistory
from .goal_scope import AllGoals, GoalScope, OneGoal
from .goal_waits import (
    GoalInputReview,
    GoalWait,
    GoalWaitRecovery,
    GoalWaitReview,
    GoalWaits,
    StandbyReview,
)
from .native_input_owner import GoalLaunchOwner
from .owner_requests import NewestOpenRequest, RequestDirective
from .registration import Registration

if TYPE_CHECKING:
    from .agent_events import GoalChange
    from .messages import Message
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
from .errors import TodoError
from .todos import SendBackVerdict, StrikeReason, Todo, JudgedCall, TodoBatch, TodoCommand, TodoStore

_LOG = logging.getLogger(__name__)
HistoryReply = TypeVar("HistoryReply", bound=RecordedGoalHistory)


class Goals(GoalWaitRecovery):
    def __init__(self, root: Path, registry: Registration, bus: MessageBus, agents: AgentActivity):
        self.root = root
        self.registry = registry
        self.bus = bus
        self.agents = agents
        self.waits = GoalWaits(root)
        self.dispositions = InputDispositions(root)
        self.goal_store: GoalAttemptFile = GoalAttemptFile(GoalAttemptStore.path_in(
            GoalAttemptStore.directory_in(root)))

    def fork_goal(self, parent: Thread, child: Thread) -> Goal:
        """Create a commander's child goal from its parent's active work."""
        text = ('\n'.join(goal.text for goal in parent.goal.active())
                or f'Coordinate assigned work for {parent.name}.')
        return SetGoalAction(text=text).create(GoalActionContext(self, child))

    @cached_property
    def todos(self) -> TodoStore:
        return TodoStore(self.root)

    def change_problem(self, problem, operation, actor: ThreadIncarnation):
        """The caller's resolved identity carries its authority; a name lookup would drop it."""
        current = self.todos.get(problem)
        current.require_commander(actor)
        return operation.prepare(self, current).record(self, problem)

    def judge_calls(self, verdicts: Sequence[JudgedCall],
                    actor: ThreadIncarnation) -> tuple[Todo, ...]:
        """Check every verdict on its order's state as the earlier ones leave it, list every
        refusal together, and write only when none is refused."""
        staged, ready = TodoBatch(), TodoBatch()
        for call in verdicts:
            call.operation.staged(staged, call)
        states: dict[str, Todo] = {}
        read_at: dict[str, int] = {}
        refusals: list[str] = []
        for call in staged.ordered():
            problem, verdict = call.problem, call.operation
            # Every verdict on a problem is built from the revision its judge read; the first
            # checks that revision against the store and the rest follow the ones before them.
            if problem not in states:
                states[problem] = self.todos.get(problem)
                read_at[problem] = verdict.expected_revision
            elif verdict.expected_revision == read_at[problem]:
                verdict = replace(verdict, expected_revision=states[problem].revision)
            try:
                states[problem].require_commander(actor)
                prepared = verdict.prepare(self, states[problem])
                states[problem] = prepared.apply(states[problem])
            except TodoError as refused:
                refusals.append(f"{verdict.tool_call_id}: {refused}")
                continue
            prepared.staged(ready, replace(call, operation=prepared))
        if refusals:
            raise TodoError("; ".join(refusals))
        return ready.record(self)

    def apply_problem(self, problem: str, operation: TodoCommand) -> Todo:
        return self.todos.apply(problem, operation)

    def send_back(self, problem: str, verdict: SendBackVerdict) -> Todo:
        """The verdict and strike land on the Todo in the write that revises the holder's Goal."""
        holder = verdict.previous
        self.update_goal(holder.owner, SentBackOrderGoalAction(
            expect=GoalPrecondition(goal_id=OneGoal(holder.generation)), problem=problem, operation=verdict))
        sent_back: Todo = self.todos.get(problem)
        return sent_back

    def fire(self, unit: str, reason: StrikeReason, actor: ThreadIncarnation) -> tuple[Todo, ...]:
        """Refuse while a held problem's checkout retains work: the same owner the WORK HELD
        standing rule reads, so a release never strands what the heartbeat would protect."""
        owner = self.registry.require(unit).incarnation
        store: TodoStore = self.todos
        rule = self.registry.storage_rule
        held = tuple(work for todo in store.held_by(actor.commanding_identity, owner)
                     for work in rule.work_for_problem(todo, self.registry, store))
        if held:
            raise TodoError("; ".join(
                f"{work.owner.name} retains work in {work.checkout}" for work in held)
                + " -> the holder must commit and push it before the problem is released")
        return store.fire(owner, actor, reason)

    def open_goal_store(self) -> GoalAttemptStore:
        store = self.goal_store.open()
        self.goal_store = store
        return store

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
        entry = self.registry.entry(name).entry
        rows = self.dispositions.unresolved((entry,)).unknown(entry.names)
        return tuple(UnresolvedInput.of(row) for row in rows)

    def input_delivery(
        self,
        name: str,
        *,
        include_history: bool = False,
        scope: InputDeliveryScope = UNOBSERVED_DELIVERY_SCOPE,
    ) -> DeliveryOverview:
        """Project durable notices using current owner queue facts when available."""
        entry = self.registry.entry(name).entry
        return self.dispositions.unresolved((entry,)).delivery_overview(
            entry.names, include_history=include_history, scope=scope)

    def dismiss_historical_inputs(
        self, name: str, *, scope: InputDeliveryScope = UNOBSERVED_DELIVERY_SCOPE
    ) -> DeliveryOverview:
        """Dismiss only historical notices; UNKNOWN remains unresolved and unreplayable."""
        return self.dispositions.dismiss_historical(
            (self.registry.entry(name).entry,), scope=scope)

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
        dispositions = self.dispositions
        unknown = {row.key: row for row in
                   dispositions.unresolved((snapshot.entry(thread.name),)).unknown(owners)}
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
        self, name: str, scope: GoalScope = AllGoals()
    ) -> tuple[GoalHistoryEntry, ...]:
        """Read durable revisions for one thread incarnation within ``scope``."""
        return self.registry.goal_history(name, scope)

    def recorded_history(
        self, reply: type[HistoryReply], name: str, scope: GoalScope
    ) -> HistoryReply:
        """Revisions from goal history, execution failures from the attempt ledger."""
        owner = self.registry.require(name).incarnation.wire_birth
        return reply(
            tuple(entry.recorded() for entry in self.goal_history(name, scope)),
            self.open_goal_store().failures(owner, scope),
        )

    def goal_execution(self, name: str) -> GoalExecution:
        return self._goal_snapshot(name).execution

    def goal_changed(self, name: str, previous: GoalPublicationSnapshot) -> GoalChange:
        """Compare a rebuilt view after its publication owner observed a source change."""
        current = self.goal_snapshot(name)
        return GoalChanged(current) if previous.changed_by(current) else GoalUnchanged()

    def goal_snapshot(self, name: str) -> GoalSnapshot:
        """Read current goal and its scheduling projection as one owner snapshot."""
        return self._goal_snapshot(name)

    def _goal_snapshot(self, name: str) -> GoalSnapshot:
        snapshot = self.registry.snapshot()
        canonical = snapshot.canonical_name(name)
        # One owner's wait is one keyed row; decoding every wait to pick it cost 70% of each
        # goal_changed, once per hosted session on every goal store change (2026-10-30).
        return self.snapshot_for(snapshot.threads[canonical], snapshot, self.waits.resolve)

    def snapshot_for(self, owner: Thread, snapshot: RegistrySnapshot,
                     wait: Callable[[GoalDeclaration], GoalWait]) -> GoalSnapshot:
        """All goal readers build the same source from the original records."""
        for goal in owner.goal:
            store = self.open_goal_store()
            # Generation and its failure evidence come from one cut. Permit
            # attestation keeps GoalAttemptStore's independent fresh readback.
            with store.reader_snapshot() as connection:
                generation = Generation.require_in(connection, goal.id)
                source = GoalExecutionSource(
                    goal, wait(goal), generation,
                    generation.lifecycle.evidence(connection, generation),
                    owner, snapshot, self.agents,
                )
                return GoalSnapshot(goal, source.execution())
        return GoalSnapshot(owner.goal, IdleGoalExecution())

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
        with self.registry.store.editing() as edit:
            yield action.apply(GoalPublicationContext(
                self, edit.document.require(name), actor, edit=edit,
            ))
