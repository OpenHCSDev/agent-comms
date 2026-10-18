"""Declared dependency waits; goal text and private launch grants remain authoritative elsewhere."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar

from .bus_publication import CommittedDelivery
from .goal_presentation import GoalExecution, GoalExecutionState, GoalWaitTarget, IdleGoalExecution, ThreadGoalExecution
from .goals import GoalDeclaration
from .goals import Goal
from .input_attempt import InputAttempt
from .locked_store import LockedStore, RootFile
from .registry_document import RegistrySnapshot
from .thread_identity import RecordedThreadIncarnation
from .thread_owned_state import ThreadOwnedState
from .wire_log import WireLog

if TYPE_CHECKING:
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

    def for_current(self, goal: GoalDeclaration, owner: RecordedThreadIncarnation) -> GoalWait:
        return self

    def dependency_targets(self) -> tuple[GoalWaitTarget, ...]:
        return ()

    def belongs_to_turn(self, turn) -> bool:
        return False

    def include_dependency(self, pending: list[str], name: str) -> None:
        pass

    def release_closed(self, goals, owner, snapshot, rows) -> tuple[str, ...]:
        return ()

    def consume_owned_reply(self, goals, owner, reference) -> bool:
        return False

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

    def for_current(self, goal: GoalDeclaration, owner: RecordedThreadIncarnation) -> GoalWait:
        return self if self.current_for(goal, owner) else NoGoalWait()

    def dependency_targets(self) -> tuple[GoalWaitTarget, ...]:
        return self.targets

    def belongs_to_turn(self, turn) -> bool:
        return (turn is not None and self.report_turn_id == turn.turn_id
                and self.report_turn_generation == turn.identity.generation)

    def include_dependency(self, pending: list[str], name: str) -> None:
        pending.append(name)

    def release_closed(self, goals, owner, snapshot, rows) -> tuple[str, ...]:
        closed = GoalWaits.closed_wait_group(owner.name, self.targets, rows, snapshot, goals.agents)
        if not closed or self.has_reply(goals.bus.log, owner.incarnation.require_recorded()):
            return ()
        names = ", ".join(f"@{member}" for member in closed)
        note = (f"Dependency wait group ({names}) has no independent worker. "
                "Standby was released; inspect dependencies and continue useful work.")
        goals.registry.register(replace(owner, goal=owner.goal.append_progress(note)),
                                snapshot.statuses[owner.name])
        if not goals.waits.clear(self.goal_id, wait_id=self.wait_id):
            return ()
        return closed

    def release_after_finished_turn(self, goals, owner, source_name, snapshot, goal: GoalDeclaration) -> bool:
        if (not self.current_for(goal, owner.incarnation.require_recorded())
                or any(goals.agents.is_working(target.incarnation, snapshot) for target in self.targets)
                or self.has_reply(goals.bus.log, owner.incarnation.require_recorded())):
            return False
        diagnostic = (
            f"Declared dependency @{source_name} finished without a qualifying direct "
            "reply, and no declared dependency has an active turn. "
            "Standby was released; inspect messages and UNKNOWN inputs, then "
            "continue independent work or redelegate. No input was replayed."
        )
        # Persist the explanation before clearing the wait, as one ordered release.
        goals.registry.register(replace(owner, goal=owner.goal.append_progress(diagnostic)),
                                snapshot.statuses[owner.name])
        goals.waits.clear(self.goal_id, wait_id=self.wait_id)
        return True

    def consume_owned_reply(self, goals, owner, reference) -> bool:
        with goals.bus.log.certified_read() as source:
            original = source.delivery(reference.seq)
            if original.message.reference != reference:
                raise ValueError("Dependency reply source reference changed.")
            if not self.matches(original, owner.incarnation.require_recorded()):
                return False
        return self.consume_reply(goals.waits, self.goal_id)

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


@dataclass(frozen=True, slots=True)
class GoalInputReview:
    """Ephemeral dependency review projection derived from bus and input authority."""

    goal_id: str
    targets: tuple[GoalWaitTarget, ...]
    owners: frozenset[str]
    unknown: tuple[InputAttempt, ...]
    eligible_keys: frozenset[str]

    def public(self) -> dict:
        eligible, reviewed, excluded = [], [], []
        for row in self.unknown:
            item = row.public()
            if row.key not in self.eligible_keys:
                excluded.append(
                    {
                        **item,
                        "reason": (
                            "owner_input_without_bus_sequence"
                            if row.sequence is None
                            else "not_a_direct_reply_from_declared_dependencies"
                        ),
                    }
                )
            elif row.reviewed_for_goal(self.goal_id):
                reviewed.append(item)
            else:
                eligible.append(item)
        return {
            "goal_id": self.goal_id,
            "wait_for": [target.incarnation.name for target in self.targets],
            "reviewed_inputs": [item["inputId"] for item in eligible],
            "messages": eligible,
            "already_reviewed_inputs": reviewed,
            "excluded_inputs": excluded,
        }


@dataclass(frozen=True, slots=True)
class GoalWaits(RootFile, ThreadOwnedState, LockedStore[dict[str, RecordedGoalWait]]):
    lock_rank = 9
    filename: ClassVar[str] = "goal_waits.json"

    @property
    def record_type(self) -> type[dict[str, RecordedGoalWait]]:
        return dict[str, RecordedGoalWait]

    def empty(self) -> dict[str, RecordedGoalWait]:
        return {}

    def record(self, wait: RecordedGoalWait) -> None:
        self.update(lambda rows: {**rows, wait.goal_id: wait})

    def remove_threads(self, threads: Sequence[Thread]) -> None:
        """A wait belongs to its owner's goal, which ends with the declaration."""
        owners = {thread.incarnation.require_recorded().lookup for thread in threads}
        self.update(
            lambda rows: (
                {key: row for key, row in rows.items() if row.owner.lookup not in owners}
                if any(row.owner.lookup in owners for row in rows.values())
                else rows
            )
        )

    def clear(self, goal_id: str, *, wait_id: str | None = None) -> bool:
        removed = False

        def change(rows: dict[str, RecordedGoalWait]) -> dict[str, RecordedGoalWait]:
            nonlocal removed
            current = rows.get(goal_id)
            if current is None or (wait_id is not None and current.wait_id != wait_id):
                return rows
            removed = True
            return {key: row for key, row in rows.items() if key != goal_id}

        self.update(change)
        return removed

    @staticmethod
    def closed_wait_group(
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
            wait = thread.goal.current_wait(rows, thread.incarnation.require_recorded())
            dependencies = targets if name == owner else wait.dependency_targets()
            for target in dependencies:
                canonical = snapshot.canonical_name(target.incarnation.name)
                peer = snapshot.threads.get(canonical)
                if peer is None or target.incarnation.resolved(snapshot) != peer.incarnation:
                    continue
                if canonical == owner:
                    pending.append(owner)
                    continue
                peer_wait = peer.goal.current_wait(rows, peer.incarnation.require_recorded())
                if agents.is_working(target.incarnation, snapshot):
                    # A persisted wait does not make a *new* live owner turn
                    # part of the old wait graph. It may send the reply before
                    # its turn finishes; only the reporting turn belongs here.
                    current_turn = peer.turn_lease
                    if not peer_wait.belongs_to_turn(current_turn):
                        return ()
                peer_wait.include_dependency(pending, canonical)
        return tuple(sorted(seen))
