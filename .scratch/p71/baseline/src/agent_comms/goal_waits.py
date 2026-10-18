"""Declared dependency waits; goal text and private launch grants remain authoritative elsewhere."""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from .bus_publication import CommittedDelivery
from .goal_presentation import GoalExecution, GoalExecutionState, GoalWaitTarget
from .goals import Goal
from .input_attempt import InputAttempt
from .locked_store import LockedStore
from .registry_document import RegistrySnapshot
from .thread_identity import RecordedThreadIncarnation
from .thread_owned_state import ThreadOwnedState
from .wire_log import WireLog

if TYPE_CHECKING:
    from .threads import Thread


class GoalWait(ABC):
    """A resolved dependency state; only RecordedGoalWait is persisted."""

    @abstractmethod
    def allows_routed_input(self) -> bool: ...

    def matches_id(self, wait_id: str) -> bool:
        return False

    def owner_matches(self, created_at: float) -> bool:
        return False

    def matches(self, original: CommittedDelivery, owner: RecordedThreadIncarnation) -> bool:
        return False

    def consume_dependency(self, comms, canonical: str) -> bool:
        return True

    def consume_reply(self, waits: GoalWaits, goal_id: str) -> bool:
        return False

    def project(self, goal: Goal, snapshot: RegistrySnapshot) -> GoalExecution:
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
    owner_created_at: float
    # An idle dependency has no turn generation at the moment of declaration.
    target_turn_generations: tuple[int | None, ...]
    # Runtime declarations outside a turn explicitly carry no reporting turn.
    report_turn_id: str | None
    report_turn_generation: int | None

    def __post_init__(self) -> None:
        if (
            type(self.owner_created_at) not in (int, float)
            or not math.isfinite(self.owner_created_at)
            or self.owner_created_at <= 0
            or len(self.target_turn_generations) != len(self.targets)
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

    def owner_matches(self, created_at: float) -> bool:
        return self.owner_created_at == created_at

    def consume_dependency(self, comms, canonical: str) -> bool:
        return comms.goals.consume_goal_wait(canonical, self.wait_id)

    def consume_reply(self, waits: GoalWaits, goal_id: str) -> bool:
        return waits.clear(goal_id, wait_id=self.wait_id)

    def project(self, goal: Goal, snapshot: RegistrySnapshot) -> GoalExecution:
        targets = tuple(
            GoalWaitTarget(target.incarnation.renamed(snapshot.canonical_name(target.incarnation.name)))
            for target in self.targets
        )
        inactive = tuple(
            target for target in targets
            if not GoalWaits.target_has_active_turn(target, snapshot)
        )
        return GoalExecution(GoalExecutionState.STANDBY, goal.id, targets, inactive)

    def current_for(self, goal: Goal, owner_created_at: float) -> bool:
        """A retained wait can survive edits, but not replacement of its owner.

        Callers select the row by goal ID before asking this question. A newer
        goal revision preserves the declared wait; a future wait revision does
        not belong to the observed goal snapshot.
        """
        return self.owner_created_at == owner_created_at and goal.accepts_observation(
            self.goal_id, self.revision
        )

    def matches(self, original: CommittedDelivery, owner: RecordedThreadIncarnation) -> bool:
        """Join original sender and the actual addressed owner through its source."""
        if not self.owner_matches(owner.created_at):
            return False
        return (
            original.message.seq > self.after_seq
            and original.direct_for(owner.lookup)
            and any(target.sent(original) for target in self.targets)
        )

    def has_reply(self, bus: WireLog, owner: RecordedThreadIncarnation) -> bool:
        if not self.owner_matches(owner.created_at):
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
class GoalWaits(ThreadOwnedState, LockedStore[dict[str, RecordedGoalWait]]):
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
        goals = {thread.goal.id for thread in threads if thread.goal is not None}
        self.update(
            lambda rows: (
                {key: row for key, row in rows.items() if key not in goals}
                if goals & rows.keys()
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
    def for_goal(goal: Goal, rows: dict[str, RecordedGoalWait]) -> GoalWait:
        if not goal.state.active:
            return NoGoalWait()
        return rows.get(goal.id, NoGoalWait())

    @classmethod
    def execution(
        cls, goal: Goal | None, rows: dict[str, RecordedGoalWait], snapshot: RegistrySnapshot
    ) -> GoalExecution | None:
        """Project an optional thread goal from its resolved wait state."""
        if goal is None:
            return None
        return cls.for_goal(goal, rows).project(goal, snapshot)

    @staticmethod
    def target_has_active_turn(target: GoalWaitTarget, snapshot: RegistrySnapshot) -> bool:
        canonical = snapshot.canonical_name(target.incarnation.name)
        thread = snapshot.threads.get(canonical)
        status = snapshot.statuses.get(canonical)
        return bool(
            thread is not None
            and target.incarnation.resolved(snapshot) == thread.incarnation
            and status is not None
            and status.running
            and thread.executing
        )

    @staticmethod
    def closed_wait_group(
        owner: str,
        targets: tuple[GoalWaitTarget, ...],
        rows: dict[str, RecordedGoalWait],
        snapshot: RegistrySnapshot,
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
            goal = thread.active_goal
            wait = rows.get(goal.id) if goal is not None else None
            if wait is not None and (
                goal is None
                or not wait.current_for(goal, thread.created_at)
            ):
                wait = None
            dependencies = targets if name == owner else (wait.targets if wait is not None else ())
            for target in dependencies:
                canonical = snapshot.canonical_name(target.incarnation.name)
                peer = snapshot.threads.get(canonical)
                if peer is None or target.incarnation.resolved(snapshot) != peer.incarnation:
                    continue
                if canonical == owner:
                    pending.append(owner)
                    continue
                peer_goal = peer.active_goal
                peer_wait = (
                    rows.get(peer_goal.id)
                    if peer_goal is not None
                    else None
                )
                if peer_wait is not None and (
                    peer_goal is None
                    or not peer_wait.current_for(peer_goal, peer.created_at)
                ):
                    peer_wait = None
                if GoalWaits.target_has_active_turn(target, snapshot) and peer.process_alive:
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
