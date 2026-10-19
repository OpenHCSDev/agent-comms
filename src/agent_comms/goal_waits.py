"""Declared dependency waits; goal text and private launch grants remain authoritative elsewhere."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from .bus_publication import CommittedDelivery
from .declared_family import DeclaredFamily
from .goal_presentation import GoalExecution, GoalExecutionState, GoalWaitTarget, IdleGoalExecution, ThreadGoalExecution
from .goals import Goal, GoalDeclaration
from .input_attempt import StoredInput, UnresolvedInput
from .typed_table import Column, ForeignKey, Index, TypedStore, TypedTable
from .thread_identity import RecordedThreadIncarnation
from .thread_owned_state import ThreadOwnedState
from .wire_log import WireLog

if TYPE_CHECKING:
    from .registry_document import RegistrySnapshot
    from .agent_activity import AgentActivity
    from .threads import Thread


class GoalWait(ABC):
    """A resolved dependency state; only RecordedGoalWait is persisted."""

    @abstractmethod
    def allows_routed_input(self) -> bool: ...

    def matches_id(self, wait_id: str) -> bool:
        return False

    def owner_matches(self, owner: RecordedThreadIncarnation) -> bool:
        return False

    def matches(self, original: CommittedDelivery, owner: RecordedThreadIncarnation) -> bool:
        return False

    def review_after_seq(
        self, goal: Goal, owner: RecordedThreadIncarnation, targets: tuple[GoalWaitTarget, ...]
    ) -> int:
        return goal.published_seq

    def consume_dependency(self, comms, canonical: str) -> bool:
        return True

    def consume_reply(self, waits: GoalWaits, goal_id: str) -> bool:
        return False

    def idle_execution(self) -> IdleGoalExecution:
        return IdleGoalExecution()

    def project(self, goal: Goal, snapshot: RegistrySnapshot, agents: AgentActivity) -> GoalExecution:
        return GoalExecution(
            GoalExecutionState.for_domain_state(goal.state),
            goal.id,
            block_reason=goal.state.reason,
        )


@dataclass(frozen=True, slots=True)
class NoGoalWait(GoalWait):
    def allows_routed_input(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class RecordedGoalWait(GoalWait):
    goal_id: str
    wait_id: str
    revision: int
    after_seq: int
    targets: tuple[GoalWaitTarget, ...]
    owner: RecordedThreadIncarnation
    # An idle dependency has no turn generation at the moment of declaration.
    target_turn_generations: tuple[int | None, ...]
    # Runtime declarations outside a turn explicitly carry no reporting turn.
    report_turn_id: str | None
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
                not self.report_turn_id
                or type(self.report_turn_generation) is not int
                or self.report_turn_generation <= 0
            )
        ):
            raise ValueError("A reporting turn requires both its ID and generation.")

    def allows_routed_input(self) -> bool:
        return False

    def matches_id(self, wait_id: str) -> bool:
        return self.wait_id == wait_id

    def owner_matches(self, owner: RecordedThreadIncarnation) -> bool:
        return self.owner.same_publication_birth(owner)

    def consume_dependency(self, comms, canonical: str) -> bool:
        return comms.goals.consume_goal_wait(canonical, self.wait_id)

    def consume_reply(self, waits: GoalWaits, goal_id: str) -> bool:
        return waits.clear(goal_id, wait_id=self.wait_id)

    def project(self, goal: Goal, snapshot: RegistrySnapshot, agents: AgentActivity) -> GoalExecution:
        targets = tuple(
            GoalWaitTarget(target.incarnation.renamed(snapshot.canonical_name(target.incarnation.name)))
            for target in self.targets
        )
        inactive = tuple(
            target for target in targets
            if not agents.is_working(target.incarnation, snapshot)
        )
        return GoalExecution(GoalExecutionState.STANDBY, goal.id, targets, inactive)

    def current_for(self, goal: GoalDeclaration, owner: RecordedThreadIncarnation) -> bool:
        """A retained wait can survive edits, but not replacement of its owner.

        Callers select the row by goal ID before asking this question. A newer
        goal revision preserves the declared wait; a future wait revision does
        not belong to the observed goal snapshot.
        """
        return self.owner_matches(owner) and goal.accepts_observation(
            self.goal_id, self.revision
        )

    def matches(self, original: CommittedDelivery, owner: RecordedThreadIncarnation) -> bool:
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

    def has_reply(self, bus: WireLog, owner: RecordedThreadIncarnation) -> bool:
        if not self.owner_matches(owner):
            return False
        return any(
            self.matches(original, owner)
            for original in bus.addressed_sources(owner.lookup, self.after_seq)
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
            input_id=row.public_id, status=row.public_status,
            reviewed_for_goals=tuple(sorted(row.goal_reviews)), reason=reason)


@dataclass(frozen=True)
class StandbyReview:
    """The reply form of a goal input review: eligible messages and their exact keys."""

    goal_id: str
    wait_for: tuple[str, ...]
    reviewed_inputs: tuple[str, ...]
    messages: tuple[UnresolvedInput, ...]
    already_reviewed_inputs: tuple[UnresolvedInput, ...]
    excluded_inputs: tuple[ExcludedInput, ...]

@dataclass(frozen=True, slots=True)
class GoalInputReview:
    """Ephemeral dependency review projection derived from bus and input authority."""

    goal_id: str
    targets: tuple[GoalWaitTarget, ...]
    owners: frozenset[str]
    unknown: tuple[StoredInput, ...]
    eligible_keys: frozenset[str]

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


@dataclass(frozen=True, slots=True)
class StoredGoalWaitRow(TypedTable):
    goal_id: str = field(metadata={"sql": Column(primary_key=True)})
    wait: RecordedGoalWait


@dataclass(frozen=True, slots=True)
class GoalWaitTargetRow(TypedTable):
    goal_id: str = field(metadata={"sql": Column(primary_key=True)})
    position: int = field(metadata={"sql": Column(primary_key=True)})
    target_lookup: str
    indexes = (Index(("target_lookup", "goal_id")),)

    @classmethod
    def references(cls) -> tuple[ForeignKey, ...]:
        return (ForeignKey(("goal_id",), StoredGoalWaitRow, ("goal_id",), on_delete="CASCADE"),)


@dataclass(frozen=True, slots=True)
class GoalWaits(ThreadOwnedState, TypedStore):
    path: Path = field()
    filename: ClassVar[str] = "goal_waits.sqlite3"
    rows: ClassVar[tuple[type[TypedTable], ...]] = (StoredGoalWaitRow, GoalWaitTargetRow)

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
                StoredGoalWaitRow(wait.goal_id, wait).upsert(db)
                GoalWaitTargetRow.delete(db, goal_id=wait.goal_id)
                for position, target in enumerate(wait.targets):
                    GoalWaitTargetRow(wait.goal_id, position, target.incarnation.lookup).insert(db)

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

    def clear(self, goal_id: str, *, wait_id: str | None = None) -> bool:
        if not self.path.exists():
            return False
        with self.writing() as db:
            return StoredGoalWaitRow.delete(
                db, where="goal_id=? AND (? IS NULL OR json_extract(wait, '$.wait_id')=?)",
                parameters=(goal_id, wait_id, wait_id),
            ).rowcount > 0

    def resolve(self, goal: GoalDeclaration) -> GoalWait:
        return self.for_goal(goal, self.read())

    @staticmethod
    def for_goal(goal: GoalDeclaration, rows: dict[str, RecordedGoalWait]) -> GoalWait:
        return rows.get(goal.active().reported_id(), NoGoalWait())

    @classmethod
    def execution(
        cls,
        goal: GoalDeclaration,
        rows: dict[str, RecordedGoalWait],
        snapshot: RegistrySnapshot,
        agents: AgentActivity,
    ) -> ThreadGoalExecution:
        """Resolve absence once beside the goal/wait acquisition."""
        return goal.describe_execution(cls.for_goal(goal, rows), snapshot, agents)

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
        """Find waits with no path to a live active turn outside the wait graph.

        A dependency list wakes on any qualifying reply. One independent
        target is therefore enough to keep a group runnable, even if another
        branch contains a cycle. Call this under the wire lock before recording
        the proposed wait so concurrent standby reports cannot both pass.
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
            )), None)
            dependencies = targets if name == owner else (wait.targets if wait is not None else ())
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
                )), None)
                if agents.is_working(target.incarnation, snapshot):
                    # A persisted wait does not make a *new* live owner turn
                    # part of the old wait graph. It may send the reply before
                    # its turn finishes; only the reporting turn belongs here.
                    current_turn = peer.turn_lease
                    if (
                        peer_wait is None
                        or current_turn is None
                        or peer_wait.report_turn_id != current_turn.turn_id
                        or peer_wait.report_turn_generation != current_turn.identity.generation
                    ):
                        return ()
                if peer_wait is not None:
                    pending.append(canonical)
        return tuple(sorted(seen))
