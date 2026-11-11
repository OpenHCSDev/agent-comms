from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import cached_property

from agent_comms.context_segments.wake_values import (
    NoWakeGoalContext,
    WakeGoalContext,
    WakeWorkContext,
    WakeWorkSource,
)
from agent_comms.coordination.errors import StaleFence
from agent_comms.errors import (
    BirthChangedError,
    RelationViolationError,
    StaleAttemptError,
    TodoError,
    UnregisteredThreadError,
    UnresolvedAttemptError,
)
from agent_comms.execution.native_input_owner import GoalLaunchOwner
from agent_comms.execution.turn_lease import ActiveTurn, TurnDeclaration
from agent_comms.input_attempt import (
    UNOBSERVED_DELIVERY_SCOPE,
    InputDeliveryScope,
    UnresolvedInputReference,
)
from agent_comms.input_disposition import DeliveryOverview
from agent_comms.messaging.message_directions import MessageDirections
from agent_comms.messaging.messages import Message
from agent_comms.nominal.field_codec import GoalId
from agent_comms.planning.goal_actions import (
    GoalAction,
    GoalActionContext,
    GoalPrecondition,
    GoalPublicationContext,
    HeadReviewedOrderGoalAction,
    RuntimeInvocable,
    SentBackOrderGoalAction,
    SetGoalAction,
)
from agent_comms.planning.goal_attempts import GoalAttemptStore
from agent_comms.planning.goal_events import GoalChange, GoalChanged, GoalUnchanged
from agent_comms.planning.goal_failure_observation import GoalFailureEvidence
from agent_comms.planning.goal_history import GoalHistoryEntry, RecordedGoalHistory
from agent_comms.planning.goal_ledger import FailedTurnEvidence, Generation
from agent_comms.planning.goal_mutation import (
    GoalFailureProjection,
    GoalFork,
    GoalGenerationProjection,
    GoalReadyGrantSource,
    GoalStateProjection,
    GoalTurnProjection,
    GoalWaitProjection,
)
from agent_comms.planning.goal_permit_source import GoalStoreSource
from agent_comms.planning.goal_presentation import (
    AttemptFailedExecution,
    AttemptUnresolvedExecution,
    CompletedExecution,
    GoalExecutionControls,
    GoalPublicationSnapshot,
    GoalSnapshot,
    IdleGoalExecution,
    PausedExecution,
    RetryableBlockExecution,
    RunnableExecution,
    RunningExecution,
    StandbyExecution,
)
from agent_comms.planning.goal_records import GoalRecords
from agent_comms.planning.goal_scope import AllGoals, GoalScope, OneGoal
from agent_comms.planning.goal_snapshots import GoalSnapshots
from agent_comms.planning.goal_states import PauseSource
from agent_comms.planning.goal_wait_target import GoalWaitTarget
from agent_comms.planning.goal_waits import GoalWait, StandbyReview
from agent_comms.planning.goals import Goal, GoalDeclaration
from agent_comms.planning.order_words import StrikeReason
from agent_comms.planning.orders import (
    Assignment,
    JudgedCall,
    ProblemGoals,
    RecordOrderHeadVerdict,
    SendBackVerdict,
    Todo,
    TodoBatch,
    TodoCommand,
    TodoStore,
)
from agent_comms.planning.owner_requests import NewestOpenRequest, RequestDirective
from agent_comms.registry.agent_activity import AgentActivity
from agent_comms.registry.registry_document import RegistryDocument, RegistrySnapshot
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.registry.threads import NativeThread, Thread

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class GoalExecutionSource(
    GoalStateProjection[GoalExecutionControls, PauseSource],
    GoalFailureProjection[GoalExecutionControls, FailedTurnEvidence],
    GoalGenerationProjection[GoalExecutionControls], GoalWaitProjection[GoalExecutionControls],
    GoalTurnProjection[GoalExecutionControls, ActiveTurn],
):
    """One read's original owners; never persisted or used as launch authority."""

    goal: Goal
    wait: GoalWait
    generation: Generation
    evidence: GoalFailureEvidence[FailedTurnEvidence]
    owner: Thread
    snapshot: RegistrySnapshot
    agents: AgentActivity

    @cached_property
    def turn(self) -> TurnDeclaration:
        return self.owner.turn_record.read()

    @cached_property
    def owner_alive(self) -> bool:
        return self.owner.process_alive

    def active(self) -> GoalExecutionControls:
        return self.wait.execution(self)

    def generation_execution(self) -> GoalExecutionControls:
        return self.generation.lifecycle.execution(self)

    def idle_execution(self) -> GoalExecutionControls:
        return self.generation.lifecycle.idle_execution(self)

    def active_turn_execution(self, turn: ActiveTurn) -> GoalExecutionControls:
        if self.owner_alive and turn.owner == self.owner.process_identity:
            return RunningExecution(self.goal.id)
        return self.idle_execution()

    def standby_execution(self) -> GoalExecutionControls:
        targets = tuple(
            GoalWaitTarget(target.incarnation.renamed(self.snapshot.canonical_name(target.incarnation.name)))
            for target in self.wait.targets
        )
        inactive = tuple(
            target for target in targets
            if not self.agents.is_working(target.incarnation, self.snapshot)
        )
        return StandbyExecution(self.goal.id, targets, inactive)

    def paused(self, source: PauseSource) -> GoalExecutionControls:
        return PausedExecution(self.goal.id, source)

    def blocked(self, reason: str) -> GoalExecutionControls:
        return RetryableBlockExecution(self.goal.id, reason)

    def completed(self) -> GoalExecutionControls:
        return CompletedExecution(self.goal.id)

    def turn_execution(self) -> GoalExecutionControls:
        return self.turn.execution(self)

    def unresolved(self) -> GoalExecutionControls:
        return AttemptUnresolvedExecution(self.goal.id)

    def runnable(self) -> GoalExecutionControls:
        return RunnableExecution(self.goal.id)

    def failed(self, evidence: FailedTurnEvidence) -> GoalExecutionControls:
        return AttemptFailedExecution(self.goal.id, evidence)

    def failed_execution(self) -> GoalExecutionControls:
        return self.evidence.execution(self)

    def execution(self) -> GoalExecutionControls:
        return self.goal.state.execution(self)


class GoalTarget(ABC):
    """Which thread a goal write selects inside the registry edit that publishes it."""

    @property
    @abstractmethod
    def name(self) -> str:
        """The thread name this write is addressed to."""

    @abstractmethod
    def select(self, document: RegistryDocument) -> Thread:
        """The thread this write applies to, read under the edit's lock."""


@dataclass(frozen=True)
class NamedGoalTarget(GoalTarget):
    """Whichever thread holds the name when the edit opens."""

    addressed: str

    @property
    def name(self) -> str:
        return self.addressed

    def select(self, document: RegistryDocument) -> Thread:
        return document.require(self.addressed)


@dataclass(frozen=True)
class BornGoalTarget(GoalTarget):
    """Exactly one birth: a decision made for it never lands on a thread that replaced it
    under the same name before the write."""

    birth: ThreadIncarnation

    @property
    def name(self) -> str:
        return self.birth.name

    def select(self, document: RegistryDocument) -> Thread:
        """The name follows renames (aliases); a removed or replaced birth is refused alike."""
        try:
            thread = document.require(self.birth.name)
        except UnregisteredThreadError as error:
            raise BirthChangedError() from error
        if thread.incarnation != self.birth:
            raise BirthChangedError()
        return thread


class Goals(
    GoalRecords,
    ProblemGoals,
    WakeWorkSource[Thread],
    GoalFork[Thread, GoalDeclaration],
    MessageDirections[Message],
    GoalStoreSource[GoalAttemptStore],
    GoalReadyGrantSource[NativeThread, Generation],
    GoalSnapshots[Thread, GoalSnapshot, RegistrySnapshot],
):
    def fork_goal(self, parent: Thread, child: Thread) -> Goal:
        """Create a teammate's child goal from its parent's active work."""
        text = ('\n'.join(goal.text for goal in parent.goal.active())
                or f'Coordinate assigned work for {parent.name}.')
        return SetGoalAction(text=text).create(GoalActionContext(self, child))

    def record_order_review(self, holder: Assignment, current: Todo,
                            operation: RecordOrderHeadVerdict) -> None:
        """Record this holder's review through its original goal generation."""
        self.update_goal(holder.owner, HeadReviewedOrderGoalAction(
            expect=GoalPrecondition(goal_id=OneGoal(holder.generation)),
            problem=current.id, operation=operation,
        ))

    def change_problem(self, problem: str, operation: TodoCommand,
                       actor: ThreadIncarnation) -> Todo:
        """The caller's resolved identity carries its authority; a name lookup would drop it."""
        current = self.todos.get(problem)
        current.require_maintainer(actor)
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
                states[problem].require_maintainer(actor)
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
        held = tuple(work for todo in store.held_by(owner)
                     for work in rule.work_for_problem(todo, self.registry, store))
        if held:
            raise TodoError("; ".join(
                f"{work.owner.name} retains work in {work.checkout}" for work in held)
                + " -> the holder must commit and push it before the problem is released")
        return store.fire(owner, actor, reason)

    def ready_goal_grant_locked(
        self, owner: NativeThread, claim_scope: int, generation: Generation
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

    def unresolved_inputs(self, name: str) -> tuple[UnresolvedInputReference, ...]:
        """Project durable unresolved inputs by reference; reading never schedules another
        attempt, and each body stays in the bus under its sequence."""
        entry = self.registry.entry(name).entry
        rows = self.dispositions.unresolved((entry,)).unknown(entry.names)
        return tuple(UnresolvedInputReference.of(row) for row in rows)

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

    def goal_history(
        self, name: str, scope: GoalScope = AllGoals()
    ) -> tuple[GoalHistoryEntry, ...]:
        """Read durable revisions for one thread incarnation within ``scope``."""
        return self.registry.goal_history(name, scope)

    def recorded_history(self, name: str, scope: GoalScope) -> RecordedGoalHistory:
        """Revisions from goal history, execution failures from the attempt ledger."""
        owner = self.registry.require(name).incarnation.wire_birth
        return RecordedGoalHistory(
            tuple(entry.recorded() for entry in self.goal_history(name, scope)),
            self.open_goal_store().failures(owner, scope),
        )

    def wake_context(self, owner: Thread) -> WakeWorkContext:
        """The native wake carries the same effective lifecycle as every goal reader."""
        snapshot = self.snapshot_for(owner, self.registry.snapshot(), self.waits.resolve)
        current_goal = next((WakeGoalContext(
            text=goal.order.goal_text(goal), execution=snapshot.execution,
            progress=goal.order.progress(
                goal, goal.order.todos(self, owner.incarnation, goal.id)),
        ) for goal in snapshot.goal), NoWakeGoalContext())
        return WakeWorkContext(
            name=owner.name, title=owner.title, tags=tuple(sorted(owner.tags)),
            original_assignment=owner.task, current_goal=current_goal,
        )

    def goal_execution(self, name: str) -> GoalExecutionControls:
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
                    goal=goal, wait=wait(goal), generation=generation,
                    evidence=generation.lifecycle.evidence(connection, generation),
                    owner=owner, snapshot=snapshot, agents=self.agents,
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
        with self._updating_goal(NamedGoalTarget(name), action, actor=actor) as goal:
            return goal

    def update_born_goal[Result: GoalDeclaration](
        self,
        birth: ThreadIncarnation,
        action: GoalAction[Result],
        *,
        actor: type = RuntimeInvocable,
    ) -> Result:
        """Update exactly this birth's goal: the registry edit refuses a thread replaced under
        its name between the decision and the write (BirthChangedError)."""
        with self._updating_goal(BornGoalTarget(birth), action, actor=actor) as goal:
            return goal

    def update_goal_snapshot[Result: GoalDeclaration](
        self, name: str, action: GoalAction[Result], *, actor: type,
    ) -> GoalSnapshot:
        """Return this publication and its schedule before another owner can change it."""
        with self._updating_goal(NamedGoalTarget(name), action, actor=actor):
            return self._goal_snapshot(name)

    @contextmanager
    def _updating_goal[Result: GoalDeclaration](
        self, target: GoalTarget, action: GoalAction[Result], *, actor: type,
    ) -> Iterator[Result]:
        """Run checks before locking, then recheck the original goal at publication."""
        action.require_actor(actor)
        action = action.prepare(self, target.name, actor)
        with self.registry.store.editing() as edit:
            yield action.apply(GoalPublicationContext(
                self, target.select(edit.document), actor, edit=edit,
            ))
