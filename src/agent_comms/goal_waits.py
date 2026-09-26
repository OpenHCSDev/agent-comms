"""Declared dependency waits; goal text and private launch grants remain authoritative elsewhere."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .declarations import (
    Goal,
    GoalExecution,
    GoalExecutionState,
    GoalWaitTarget,
    Message,
    RegistrySnapshot,
    _atomic_write_text,
    _store_lock,
)


@dataclass(frozen=True, slots=True)
class GoalWait:
    goal_id: str
    wait_id: str
    revision: int
    after_seq: int
    targets: tuple[GoalWaitTarget, ...]
    owner_created_at: float | None = None
    # Positional with targets. Legacy waits cannot attest a terminal callback.
    target_turn_generations: tuple[int | None, ...] = ()

    def matches(self, message: Message, snapshot: RegistrySnapshot) -> bool:
        sender = snapshot.threads.get(snapshot.aliases.get(message.sender, message.sender))
        return (
            sender is not None
            and message.seq > self.after_seq
            and any(
                snapshot.aliases.get(target.name, target.name) == sender.name
                and target.created_at == sender.created_at
                for target in self.targets
            )
        )


@dataclass(frozen=True, slots=True)
class GoalInputReview:
    """Ephemeral dependency review projection derived from bus and input authority."""

    goal_id: str
    targets: tuple[GoalWaitTarget, ...]
    owners: frozenset[str]
    senders: frozenset[str]
    unknown: tuple[dict, ...]
    eligible_keys: frozenset[str]

    def public(self) -> dict:
        from .input_disposition import InputDispositions

        eligible, reviewed, excluded = [], [], []
        for row in self.unknown:
            item = InputDispositions.public(row)
            if row["key"] not in self.eligible_keys:
                excluded.append(
                    {
                        **item,
                        "reason": (
                            "owner_input_without_bus_sequence"
                            if row["sequence"] is None
                            else "not_a_direct_reply_from_declared_dependencies"
                        ),
                    }
                )
            elif InputDispositions.reviewed_for_goal(row, self.goal_id):
                reviewed.append(item)
            else:
                eligible.append(item)
        return {
            "goal_id": self.goal_id,
            "wait_for": [target.name for target in self.targets],
            "reviewed_inputs": [item["inputId"] for item in eligible],
            "messages": eligible,
            "already_reviewed_inputs": reviewed,
            "excluded_inputs": excluded,
        }


@dataclass(frozen=True, slots=True)
class GoalWaits:
    path: Path

    def snapshot(self) -> dict[str, GoalWait]:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        return {
            key: GoalWait(
                goal_id=row["goal_id"],
                wait_id=row["wait_id"],
                revision=row["revision"],
                after_seq=row["after_seq"],
                targets=tuple(GoalWaitTarget(**target) for target in row["targets"]),
                owner_created_at=row.get("owner_created_at"),
                target_turn_generations=tuple(row.get("target_turn_generations", ())),
            )
            for key, row in data.items()
        }

    def _write(self, rows: dict[str, GoalWait]) -> None:
        _atomic_write_text(
            self.path,
            json.dumps({key: asdict(row) for key, row in rows.items()}),
            fsync_parent=True,
        )

    def record(self, wait: GoalWait) -> None:
        with _store_lock(self.path):
            rows = self.snapshot()
            rows[wait.goal_id] = wait
            self._write(rows)

    def clear(self, goal_id: str, *, wait_id: str | None = None) -> bool:
        with _store_lock(self.path):
            rows = self.snapshot()
            current = rows.get(goal_id)
            if current is None or (wait_id is not None and current.wait_id != wait_id):
                return False
            del rows[goal_id]
            self._write(rows)
            return True

    @staticmethod
    def for_goal(goal: Goal | None, rows: dict[str, GoalWait]) -> GoalWait | None:
        if goal is None or not goal.active:
            return None
        return rows.get(goal.id)

    @staticmethod
    def target_has_active_turn(target: GoalWaitTarget, snapshot: RegistrySnapshot) -> bool:
        canonical = snapshot.aliases.get(target.name, target.name)
        thread = snapshot.threads.get(canonical)
        status = snapshot.statuses.get(canonical)
        return bool(
            thread is not None
            and thread.created_at == target.created_at
            and status is not None
            and status.running
            and thread.active_turn is not None
        )

    @staticmethod
    def closed_wait_group(
        owner: str,
        targets: tuple[GoalWaitTarget, ...],
        rows: dict[str, GoalWait],
        snapshot: RegistrySnapshot,
    ) -> tuple[str, ...]:
        """Find waits with no path to a current thread outside the wait graph.

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
            goal = thread.goal
            wait = rows.get(goal.id) if goal is not None and goal.active else None
            dependencies = (
                targets
                if name == owner
                else (
                    wait.targets
                    if wait is not None and wait.owner_created_at in (None, thread.created_at)
                    else ()
                )
            )
            for target in dependencies:
                canonical = snapshot.aliases.get(target.name, target.name)
                peer = snapshot.threads.get(canonical)
                if peer is None or peer.created_at != target.created_at:
                    continue
                if canonical == owner:
                    pending.append(owner)
                    continue
                peer_goal = peer.goal
                peer_wait = (
                    rows.get(peer_goal.id) if peer_goal is not None and peer_goal.active else None
                )
                if peer_wait is None:
                    if (
                        peer_goal is not None and peer_goal.active
                        and snapshot.statuses[canonical].running
                    ) or GoalWaits.target_has_active_turn(target, snapshot):
                        return ()
                    continue
                pending.append(canonical)
        return tuple(sorted(seen))

    @staticmethod
    def execution(
        goal: Goal | None, rows: dict[str, GoalWait], snapshot: RegistrySnapshot
    ) -> GoalExecution | None:
        if goal is None:
            return None
        if wait := GoalWaits.for_goal(goal, rows):
            targets = tuple(
                replace(target, name=snapshot.aliases.get(target.name, target.name))
                for target in wait.targets
            )
            inactive = tuple(
                target
                for target in targets
                if not GoalWaits.target_has_active_turn(target, snapshot)
            )
            return GoalExecution(GoalExecutionState.STANDBY, goal.id, targets, inactive)
        return GoalExecution(
            GoalExecutionState.RUNNABLE if goal.active else GoalExecutionState(goal.status),
            goal.id,
            block_reason=goal.block_reason if goal.status == "blocked" else None,
        )
