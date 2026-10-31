"""Declared dependency waits; goal text and private launch grants remain authoritative elsewhere."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from sqlite3 import Connection
from typing import TYPE_CHECKING, Annotated, ClassVar

from .bus import BusReader
from .bus_publication import Delivered
from .declared_family import DeclaredFamily
from .errors import UnregisteredThreadError
from .field_codec import GoalId, GoalWaitId, NullMember, ThreadLookup, TurnId
from .goal_presentation import GoalExecution, GoalExecutionSource, GoalWaitTarget, StandbyExecution
from .goals import Goal, GoalDeclaration
from .input_key import InputKey, InputKeyText
from .input_attempt import StoredInput, UnresolvedInput
from .locked_store import GoalWakeFile
from .thread_identity import RecordedThreadIncarnation
from .thread_owned_state import ThreadOwnedState
from .turn_lease import TurnCompletion, TurnLease
from .typed_table import (
    Column, DurableStore, ForeignKey, FULL_SCHEMA_REBUILD, Index, SchemaRebuild,
    StoreRows, TypedTable,
)

if TYPE_CHECKING:
    from .agent_activity import AgentActivity
    from .registration import Registration
    from .message_bus import MessageBus
    from .message_reference import MessageReference
    from .native_input_owner import RegistryOwner
    from .registry_document import RegistrySnapshot
    from .registry_store import RegistryEdit
    from .threads import Thread


class GoalWait(ABC):
    """A resolved dependency state; only RecordedGoalWait is persisted."""

    @property
    @abstractmethod
    def targets(self) -> tuple[GoalWaitTarget, ...]: ...

    @abstractmethod
    def visit_group(
        self, peer: Thread, snapshot: RegistrySnapshot, agents: AgentActivity,
        pending: list[str],
    ) -> bool:
        """Report an independent peer, otherwise enqueue its own wait."""
        ...

    @abstractmethod
    def allows_routed_input(self) -> bool: ...

    def matches_id(self, wait_id: GoalWaitId) -> bool:
        return False

    def owner_matches(self, owner: RecordedThreadIncarnation) -> bool:
        return False

    def matches(self, original: Delivered, owner: RecordedThreadIncarnation) -> bool:
        return False

    def review_after_seq(
        self, goal: Goal, owner: RecordedThreadIncarnation, targets: tuple[GoalWaitTarget, ...]
    ) -> int:
        return goal.published_seq

    def clear(self, waits: GoalWaits) -> bool:
        return False

    def current_for(self, goal: Goal, owner: RecordedThreadIncarnation) -> bool:
        return False

    def has_reply(self, bus: BusReader, owner: RecordedThreadIncarnation) -> bool:
        return False

    @abstractmethod
    def execution(self, source: GoalExecutionSource) -> GoalExecution: ...


@dataclass(frozen=True, slots=True)
class NoGoalWait(GoalWait):
    def execution(self, source: GoalExecutionSource) -> GoalExecution:
        return source.generation.lifecycle.execution(source)

    @property
    def targets(self) -> tuple[GoalWaitTarget, ...]:
        return ()

    def visit_group(
        self, peer: Thread, snapshot: RegistrySnapshot, agents: AgentActivity,
        pending: list[str],
    ) -> bool:
        return agents.answers_later(peer.incarnation, snapshot)

    def allows_routed_input(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class RecordedGoalWait(GoalWait):
    goal_id: GoalId
    wait_id: GoalWaitId
    revision: int
    after_seq: int
    targets: tuple[GoalWaitTarget, ...] = field()
    owner: RecordedThreadIncarnation
    # An idle dependency has no turn generation at the moment of declaration.
    target_turn_generations: tuple[int | None, ...]
    # Runtime declarations outside a turn explicitly carry no reporting turn.
    report_turn_id: TurnId | None
    report_turn_generation: int | None

    def __post_init__(self) -> None:
        if (
            len(self.target_turn_generations) != len(self.targets)
            or any(
                generation is not None and (type(generation) is not int or generation <= 0)
                for generation in self.target_turn_generations
            )
        ):
            raise ValueError("A wait requires its owner incarnation and aligned target turns.")
        if (self.report_turn_id is None) != (self.report_turn_generation is None) or (
            self.report_turn_id is not None
            and (
                type(self.report_turn_generation) is not int
                or self.report_turn_generation <= 0
            )
        ):
            raise ValueError("A reporting turn requires both its ID and generation.")

    def visit_group(
        self, peer: Thread, snapshot: RegistrySnapshot, agents: AgentActivity,
        pending: list[str],
    ) -> bool:
        # Only a busy turn that did not report this wait is independent.
        if agents.is_working(peer.incarnation, snapshot) and not peer.turn_lease.reports(
            self.report_turn_id, self.report_turn_generation,
        ):
            return True
        pending.append(peer.name)
        return False

    def allows_routed_input(self) -> bool:
        return False

    def matches_id(self, wait_id: GoalWaitId) -> bool:
        return self.wait_id == wait_id

    def owner_matches(self, owner: RecordedThreadIncarnation) -> bool:
        return self.owner == owner

    def clear(self, waits: GoalWaits) -> bool:
        return waits.consume(self.goal_id, self.wait_id)

    def execution(self, source: GoalExecutionSource) -> GoalExecution:
        targets = tuple(
            GoalWaitTarget(target.incarnation.renamed(source.snapshot.canonical_name(target.incarnation.name)))
            for target in self.targets
        )
        inactive = tuple(
            target for target in targets
            if not source.agents.is_working(target.incarnation, source.snapshot)
        )
        return StandbyExecution(source.goal.id, targets, inactive)

    def current_for(self, goal: GoalDeclaration, owner: RecordedThreadIncarnation) -> bool:
        """A retained wait can survive edits, but not replacement of its owner.

        Callers select the row by goal ID before asking this question. A newer
        goal revision preserves the declared wait; a future wait revision does
        not belong to the observed goal snapshot.
        """
        return self.owner_matches(owner) and goal.accepts_observation(
            self.goal_id, self.revision
        )

    def matches(self, original: Delivered, owner: RecordedThreadIncarnation) -> bool:
        """Join original sender and the actual addressed owner through its source."""
        if not self.owner_matches(owner):
            return False
        return (
            original.message.seq > self.after_seq
            and original.direct_for(owner.lookup)
            and any(target.sent(original) for target in self.targets)
        )

    def review_after_seq(self, goal, owner, targets) -> int:
        if self.owner_matches(owner) and {
            target.incarnation.lookup for target in targets
        } == {target.incarnation.lookup for target in self.targets}:
            return self.after_seq
        return super().review_after_seq(goal, owner, targets)

    def has_reply(self, bus: BusReader, owner: RecordedThreadIncarnation) -> bool:
        if not self.owner_matches(owner):
            return False
        return any(
            self.matches(original, owner)
            for original in bus.direct_replies(
                owner.lookup, after=self.after_seq, limit=1, descending=False,
                sender_lookups=tuple(target.incarnation.lookup for target in self.targets),
            )
        )


class InputExclusion(DeclaredFamily, affix="Exclusion"):
    """Why a standby review leaves an unresolved input out; its declared name is its wire value."""


class OwnerInputWithoutBusSequenceExclusion(InputExclusion):
    """The owner's own input has no bus sequence, so no dependency sent it."""


class NotADirectReplyFromDeclaredDependenciesExclusion(InputExclusion):
    """A bus input that is not a direct reply from the declared dependencies."""


@dataclass(frozen=True)
class ExcludedInput(UnresolvedInput):
    reason: type[InputExclusion] = field(kw_only=True)

    @classmethod
    def excluded(cls, row: StoredInput, reason: type[InputExclusion]) -> ExcludedInput:
        return ExcludedInput(
            sequence=row.sequence, target=row.target, text=row.source_text,
            input_id=row.key, status=row.public_status,
            reviewed_for_goals=tuple(sorted(row.goal_reviews)), reason=reason)


@dataclass(frozen=True)
class NoStandbyReview(NullMember):
    """No standby was asked for, so the page carries no review."""


@dataclass(frozen=True)
class StandbyReview:
    """The reply form of a goal input review: eligible messages and their exact keys."""

    goal_id: GoalId
    wait_for: tuple[str, ...]
    reviewed_inputs: tuple[Annotated[InputKey, InputKeyText], ...]
    messages: tuple[UnresolvedInput, ...]
    already_reviewed_inputs: tuple[UnresolvedInput, ...]
    excluded_inputs: tuple[ExcludedInput, ...]


@dataclass(frozen=True, slots=True)
class GoalWaitReview:
    """The existing registry, turn and wait observed before reading reply history."""

    snapshot: RegistrySnapshot
    owner: Thread
    wait: GoalWait
    turn: TurnLease

    @staticmethod
    def capture(goals: GoalWaitRecovery, incarnation: RecordedThreadIncarnation) -> GoalWaitReview:
        with goals.registry.store.current_snapshot() as cut:
            snapshot = cut.snapshot
            for owner in snapshot.threads_for(incarnation):
                return GoalWaitReview(
                    snapshot, owner, goals.waits.resolve(owner.goal), owner.turn_lease,
                )
            raise UnregisteredThreadError("The reviewed thread incarnation was removed.")

    def current_for(self, goals: GoalWaitRecovery) -> bool:
        """Caller holds the publication lock; no reply decoding belongs in this check."""
        snapshot = goals.registry.snapshot()
        return any(
            self._current_goal(owner)
            and owner.turn_lease == self.turn
            and goals.waits.resolve(owner.goal) == self.wait
            for owner in snapshot.threads_for(self.owner.incarnation.require_recorded())
            if snapshot.same_thread(owner, self.snapshot, self.owner)
        )

    def _current_goal(self, owner: Thread) -> bool:
        """A retained wait follows edits of its active goal, not a frozen goal value."""
        return any(
            self.wait.current_for(goal, owner.incarnation.require_recorded())
            for goal in owner.goal.active()
        )

    def require_current(self, goals: GoalWaitRecovery, owner: Thread) -> None:
        if owner.incarnation != self.owner.incarnation or not self.current_for(goals):
            raise ValueError(
                "Goal, dependency or wait changed during reply review; inspect it again."
            )

    def publish_unanswered(
        self, goals: GoalWaitRecovery, publish: Callable[[RegistryEdit], tuple[str, ...]],
    ) -> tuple[str, ...]:
        """Publish once through the checked edit after an unlocked read proves silence."""
        owner = self.owner.incarnation.require_recorded()
        review = self
        while True:
            with goals.bus.bus.reading() as source:
                replied = self.wait.has_reply(source, owner)
                through_seq = source.revision()
            if replied:
                return ()
            with goals.registry.store.editing() as edit:
                if not review.current_for(goals):
                    try:
                        review = GoalWaitReview.capture(goals, owner)
                    except UnregisteredThreadError:
                        return ()
                    if review.wait != self.wait or not review._current_goal(review.owner):
                        return ()
                    # A new owner/turn fence needs a fresh silence read for this
                    # same wait. Re-evaluate here; a terminal callback runs once.
                    continue
                # The existing indexed sequence query reads at most one metadata row.
                # Any new candidate needs another unlocked policy read, not a stale release.
                if goals.bus.bus.received_after(owner.lookup, through_seq):
                    continue
                return publish(edit)

    @property
    def goal_id(self) -> GoalId:
        return self.owner.goal.require().id

    @property
    def owners(self) -> frozenset[str]:
        return self.snapshot.names_for(self.owner.name)


@dataclass(frozen=True, slots=True)
class GoalInputReview(GoalWaitReview):
    """Reply eligibility and the exact bus cut inspected before publication."""

    through_seq: int
    targets: tuple[GoalWaitTarget, ...]
    unknown: tuple[StoredInput, ...]
    eligible_keys: frozenset[InputKey]

    def _current_goal(self, owner: Thread) -> bool:
        """Publishing an input review still requires the exact prepared goal."""
        return owner.goal == self.owner.goal

    def current_for(self, goals: GoalWaitRecovery) -> bool:
        if not super(GoalInputReview, self).current_for(goals):
            return False
        snapshot = goals.registry.snapshot()
        before = self.snapshot
        return all(any(
            snapshot.same_thread(actual, before, before.require(target.incarnation.name))
            for actual in snapshot.threads_for(target.incarnation)
        ) for target in self.targets)

    def standby_review(self) -> StandbyReview:
        eligible: list[UnresolvedInput] = []
        reviewed: list[UnresolvedInput] = []
        excluded: list[ExcludedInput] = []
        for row in self.unknown:
            if row.key not in self.eligible_keys:
                excluded.append(ExcludedInput.excluded(row, reason=(
                    OwnerInputWithoutBusSequenceExclusion if row.sequence is None
                    else NotADirectReplyFromDeclaredDependenciesExclusion)))
            elif row.reviewed_for_goal(self.goal_id):
                reviewed.append(UnresolvedInput.of(row))
            else:
                eligible.append(UnresolvedInput.of(row))
        return StandbyReview(
            self.goal_id,
            tuple(target.incarnation.name for target in self.targets),
            tuple(item.input_id for item in eligible),
            tuple(eligible),
            tuple(reviewed),
            tuple(excluded),
        )


class GoalWaitRecovery:
    """Release and consume dependency waits against their original goal and input state."""

    registry: Registration
    bus: MessageBus
    agents: AgentActivity
    waits: GoalWaits

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
        def release(edit: RegistryEdit) -> tuple[str, ...]:
            snapshot = edit.original.snapshot
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
            return closed if self._release_reviewed_wait(edit, review, note) else ()

        return review.publish_unanswered(self, release)

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
        def release(edit: RegistryEdit) -> tuple[str, ...]:
            current = edit.original.snapshot
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
            if self._release_reviewed_wait(edit, review, note):
                actual_owner, = current.threads_for(owner.incarnation.require_recorded())
                return (actual_owner.name,)
            return ()

        return review.publish_unanswered(self, release)

    def _release_reviewed_wait(self, edit: RegistryEdit, review: GoalWaitReview, note: str) -> bool:
        """Publish a validated silent wait through the review's existing registry edit."""
        snapshot = edit.original.snapshot
        owner, = snapshot.threads_for(review.owner.incarnation.require_recorded())
        goal = owner.goal.require_active()
        progress = f"{goal.progress}\n\n{note}" if goal.progress else note
        change = edit.document.prepare_registration(
            replace(owner, goal=replace(goal, progress=progress, revision=goal.revision + 1)),
            snapshot.status(owner.name), new_owner=False,
        )
        self.registry._commit_registration(edit, change)
        return review.wait.clear(self.waits)

    def consume_reply_wait(self, owner: RegistryOwner, reference: MessageReference) -> bool:
        """Read the exact completed delivery before checking its original owner fence."""
        with self.registry.store.current_snapshot():
            owner.require_registry(self.registry)
            review = GoalWaitReview.capture(self, owner.thread.incarnation.require_recorded())
        incarnation = review.owner.incarnation.require_recorded()
        if not review.wait.owner_matches(incarnation):
            return False
        original, = self.bus.bus.deliveries_for_references((reference,))
        if not review.wait.matches(original, incarnation):
            return False
        with self.registry.store.current_snapshot() as cut:
            owner.require_registry(self.registry)
            if not review.current_for(self):
                return False
            current, = cut.snapshot.threads_for(incarnation)
            goal = current.continuation_goal(owner.thread)
            return any(review.wait.current_for(actual, incarnation)
                       and review.wait.clear(self.waits) for actual in goal)


class GoalWaitsRows(StoreRows):
    """The tables of GoalWaits; each row declares itself one."""


@dataclass(frozen=True, slots=True)
class StoredGoalWaitRow(GoalWaitsRows, TypedTable):
    goal_id: Annotated[GoalId, Column(primary_key=True)]
    wait: RecordedGoalWait

    def record_targets(self, db: Connection) -> None:
        """The stored wait owns the searchable projection of its original targets."""
        GoalWaitTargetRow.delete(db, goal_id=self.goal_id)
        for position, target in enumerate(self.wait.targets):
            GoalWaitTargetRow(self.goal_id, position, target.incarnation.lookup).insert(db)


@dataclass(frozen=True, slots=True)
class GoalWaitTargetRow(GoalWaitsRows, TypedTable):
    goal_id: Annotated[GoalId, Column(primary_key=True)]
    position: Annotated[int, Column(primary_key=True)]
    target_lookup: ThreadLookup
    indexes = (Index(("target_lookup", "goal_id")),)

    @classmethod
    def references(cls) -> tuple[ForeignKey, ...]:
        return (ForeignKey(columns=("goal_id",), target=StoredGoalWaitRow, target_columns=("goal_id",), on_delete="CASCADE"),)


@dataclass(frozen=True, slots=True)
class GoalWaits(ThreadOwnedState, DurableStore, GoalWakeFile):
    root: Path
    filename: ClassVar[str] = "goal_waits.sqlite3"
    tables = GoalWaitsRows

    @classmethod
    def carry_schema(
        cls, db: Connection, sharing: tuple[type[TypedTable], ...] = (),
        *, rebuild: SchemaRebuild = FULL_SCHEMA_REBUILD,
    ) -> None:
        super().carry_schema(db, sharing, rebuild=rebuild)
        # Only this materialized index is replaceable; the saved wait records are original.
        GoalWaitTargetRow.delete(db)
        for stored in StoredGoalWaitRow.select(db):
            stored.record_targets(db)

    def read(self) -> dict[str, RecordedGoalWait]:
        with self.snapshot() as db:
            return {
                row.goal_id: row.wait
                for row in StoredGoalWaitRow.read(
                    db.execute(f"SELECT * FROM {StoredGoalWaitRow.declared_name}")
                )
            }

    def record(self, wait: RecordedGoalWait) -> None:
        self.record_many((wait,))

    def record_many(self, waits: Iterable[RecordedGoalWait]) -> None:
        with self.writing() as db:
            for wait in waits:
                stored = StoredGoalWaitRow(wait.goal_id, wait)
                stored.upsert(db)
                stored.record_targets(db)

    def targeting(
        self, target: RecordedThreadIncarnation
    ) -> tuple[RecordedGoalWait, ...]:
        with self.snapshot() as db:
            return tuple(
                row.wait
                for row in StoredGoalWaitRow.read(
                    db.execute(
                        f"SELECT * FROM {StoredGoalWaitRow.declared_name} WHERE goal_id IN "
                        f"(SELECT goal_id FROM {GoalWaitTargetRow.declared_name} "
                        "WHERE target_lookup=?)",
                        (target.lookup,),
                    )
                )
            )

    def remove_threads(self, threads: Sequence[Thread]) -> None:
        """A wait belongs to its owner's goal, which ends with the declaration."""
        owners = {thread.incarnation.require_recorded().lookup for thread in threads}
        with self.writing() as db:
            for row in StoredGoalWaitRow.read(
                db.execute(f"SELECT * FROM {StoredGoalWaitRow.declared_name}")
            ):
                if row.wait.owner.lookup in owners:
                    StoredGoalWaitRow.delete(db, goal_id=row.goal_id)

    def clear(self, goal_id: GoalId) -> bool:
        if not self.path.exists():
            return False
        with self.writing() as db:
            return StoredGoalWaitRow.delete(db, goal_id=goal_id).rowcount > 0

    def consume(self, goal_id: GoalId, wait_id: GoalWaitId) -> bool:
        if not self.path.exists():
            return False
        with self.writing() as db:
            return StoredGoalWaitRow.delete(
                db, where="goal_id=? AND json_extract(wait, '$.wait_id')=?",
                parameters=(goal_id, wait_id),
            ).rowcount > 0

    def resolve(self, goal: GoalDeclaration) -> GoalWait:
        with self.snapshot() as db:
            for row in StoredGoalWaitRow.select(
                db, where="{goal_id} = ?", parameters=(goal.active().reported_id(),),
            ):
                return row.wait
        return NoGoalWait()

    @staticmethod
    def for_goal(goal: GoalDeclaration, rows: dict[str, RecordedGoalWait]) -> GoalWait:
        return rows.get(goal.active().reported_id(), NoGoalWait())

    @staticmethod
    def current_waits(goal: GoalDeclaration, rows: dict[str, RecordedGoalWait],
                      owner: RecordedThreadIncarnation) -> tuple[RecordedGoalWait, ...]:
        wait = rows.get(goal.reported_id())
        return (wait,) if wait is not None and wait.current_for(goal, owner) else ()

    @classmethod
    def closed_wait_group(
        cls,
        owner: str,
        targets: tuple[GoalWaitTarget, ...],
        rows: dict[str, RecordedGoalWait],
        snapshot: RegistrySnapshot,
        agents: AgentActivity,
    ) -> tuple[str, ...]:
        """Find waits closed by dead targets or waits leading back into the group.

        A live target with no wait of its own is an independent worker even
        while idle between turns. One independent target keeps the group open,
        even if another branch cycles. Call under the wire lock before recording
        a proposed wait so concurrent standby reports cannot both pass.
        """
        pending = [owner]
        seen: set[str] = set()
        while pending:
            name = pending.pop()
            if name in seen:
                continue
            seen.add(name)
            thread = snapshot.threads[name]
            goal = thread.goal.active()
            wait = next(iter(cls.current_waits(
                goal, rows, thread.incarnation.require_recorded(),
            )), NoGoalWait())
            dependencies = targets if name == owner else wait.targets
            for target in dependencies:
                canonical = snapshot.canonical_name(target.incarnation.name)
                peer = snapshot.threads.get(canonical)
                if peer is None or target.incarnation.resolved(snapshot) != peer.incarnation:
                    continue
                if canonical == owner:
                    pending.append(owner)
                    continue
                peer_goal = peer.goal.active()
                peer_wait = next(iter(cls.current_waits(
                    peer_goal, rows, peer.incarnation.require_recorded(),
                )), NoGoalWait())
                if peer_wait.visit_group(peer, snapshot, agents, pending):
                    return ()
        return tuple(sorted(seen))
