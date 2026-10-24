"""Carry retired attempt text into declared families, then delete this step."""

import re
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection
from typing import ClassVar

from agent_comms.attempt_resolution import (
    AttemptResolution,
    ComplianceDecision,
    FailedTurnResolution,
    OperatorStopped,
    OrderDecision,
    RetryDecision,
    WorkDecision,
)
from agent_comms.bus import Bus
from agent_comms.bus_tables import MessageIdentitySource
from agent_comms.field_codec import GoalId, MessageId, RetryDecisionId
from agent_comms.goal_attempts import AttemptRecord, GoalAttemptStore, GoalWorkDecision
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.todos import Todo, TodoStore
from agent_comms.turn_failure import BackendFailed
from agent_comms.typed_table import TypedRow


@dataclass(frozen=True)
class StoredResolution(TypedRow):
    key: str
    resolution: str

    def carried(self) -> AttemptResolution | None:
        if self.resolution == "Operator stopped goal turn.":
            return OperatorStopped()
        if self.resolution in (
            "backend_failed",
            "Goal turn ended without verified terminal progress.",
        ):
            # The owner's explicit cutover decision maps the retired generic text
            # to EndedWithoutResult.failure_kind (turn_terminal.py), BackendFailed.
            return FailedTurnResolution(BackendFailed)
        return None


@dataclass(frozen=True)
class StoredDecision(TypedRow):
    goal_id: GoalId
    decision_id: str
    generation: int

    def carried(self, bus: Connection, orders: set[str]) -> WorkDecision | None:
        if re.fullmatch("[0-9a-f]{32}", self.decision_id):
            return RetryDecision(RetryDecisionId(self.decision_id))
        if not re.fullmatch("[0-9a-f]{12}", self.decision_id):
            raise ValueError(f"Unknown legacy decision shape: {self}")
        answer = MessageIdentitySource.exists(bus, message_id=MessageId(self.decision_id))
        order = self.decision_id in orders
        if answer and order:
            raise ValueError(f"Both an order and a message own legacy decision: {self}")
        if answer:
            return ComplianceDecision(MessageId(self.decision_id))
        if order:
            return OrderDecision(self.decision_id)
        return None


class AttemptResolutions(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (GoalAttemptStore, TodoStore, Bus)

    def carry(self, *, apply: bool) -> Carried:
        column, = AttemptRecord._fields_named(("resolution",))
        decision_column, = GoalWorkDecision._fields_named(("decision",))
        counts: Counter[str] = Counter()
        rewritten: list[Path] = []
        changed = 0
        for path in self.input_files():
            with closing(GoalAttemptStore.read_snapshot(path)) as db:
                if not AttemptRecord.present(db):
                    continue
                legacy = tuple(StoredResolution.read(db.execute(AttemptRecord._sql(
                    f'SELECT {{reservation}} AS key,{{resolution}} '
                    f'FROM "{AttemptRecord.declared_name}" '
                    'WHERE {resolution} IS NOT NULL AND CASE '
                    "WHEN json_valid({resolution}) THEN json_type({resolution})!='object' "
                    'ELSE 1 END'
                ))))
                updates: list[tuple[object, object]] = []
                for row in legacy:
                    resolution = row.carried()
                    label = type(resolution).declared_name if resolution is not None else "nulled"
                    counts[label] += 1
                    updates.append((column.encode(resolution), row.key))
                decisions = ()
                if "decision_id" in GoalWorkDecision.stored_columns(db):
                    decisions = tuple(StoredDecision.read(db.execute(
                        f'SELECT goal_id,decision_id,generation '
                        f'FROM "{GoalWorkDecision.declared_name}"'
                    )))
            mapped: dict[str, object] = {}
            dropped: list[StoredDecision] = []
            if decisions:
                root = GoalAttemptStore.bus_root(path)
                todo_path = TodoStore.path_in(TodoStore.directory_in(root))
                bus_path = Bus.path_in(Bus.directory_in(root))
                for dependency in (todo_path, bus_path):
                    self.require_confined(self.root, dependency)
                with (
                    closing(TodoStore.read_snapshot(todo_path)) as todos,
                    closing(Bus.read_snapshot(bus_path)) as bus,
                ):
                    orders = {
                        order.id for todo in Todo.select(todos) for order in todo.order_history
                    }
                    for row in decisions:
                        decision = row.carried(bus, orders)
                        if decision is None:
                            dropped.append(row)
                            counts["decisions_dropped"] += 1
                            counts[f"dropped goal={row.goal_id} id={row.decision_id} "
                                   f"generation={row.generation}"] += 1
                        else:
                            counts[f"decisions_{type(decision).declared_name}"] += 1
                            mapped[row.decision_id] = decision_column.encode(decision)
            changed += len(updates) + len(decisions)
            if apply and (updates or decisions):
                with (
                    closing(GoalAttemptStore.writer(path)) as db,
                    GoalAttemptStore.write_transaction(db),
                ):
                    if updates:
                        self.rewrite_stored(db, AttemptRecord, column, updates)
                    if decisions:
                        for row in dropped:
                            GoalWorkDecision.delete(
                                db, where="goal_id=? AND decision_id=?",
                                parameters=(row.goal_id, row.decision_id),
                            )
                        db.create_function("carried_decision", 1, mapped.__getitem__)
                        GoalWorkDecision.rebuild(
                            db, {"decision": 'carried_decision("decision_id")'},
                        )
                        db.create_function("carried_decision", 1, None)
                rewritten.append(path.relative_to(self.root))
        return Carried(changed, detail=dict(counts), rewritten=tuple(rewritten))
