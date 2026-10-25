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
    DecidedResolution,
    OperatorStopped,
    OrderDecision,
    RetryDecision,
    VerifiedTurnDecision,
    WorkDecision,
)
from agent_comms.bus import Bus
from agent_comms.bus_tables import MessageIdentitySource
from agent_comms.field_codec import FieldCodec, GoalId, MessageId, OrderId, RetryDecisionId, TurnId
from agent_comms.goal_attempts import AttemptRecord, GoalAttemptStore, GoalWorkDecision
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.todos import Todo, TodoStore
from agent_comms.turn_failure import BackendFailed, FailedTurnResolution
from agent_comms.typed_table import TypedRow


class OrphanedDecisionError(Exception):
    """A retired decision that no message or order owns."""


# The owner's explicit cutover decision maps the retired generic text
# to EndedWithoutResult.failure_kind (turn_terminal.py), BackendFailed.
RETIRED_TEXT: dict[str, AttemptResolution] = {
    "Operator stopped goal turn.": OperatorStopped(),
    "backend_failed": FailedTurnResolution(BackendFailed),
    "Goal turn ended without verified terminal progress.": FailedTurnResolution(BackendFailed),
}


@dataclass(frozen=True)
class StoredResolution(TypedRow):
    key: str
    resolution: str
    goal_id: GoalId
    generation: int

    def carried(self, bus: Connection, orders: set[OrderId]) -> AttemptResolution:
        if self.resolution in RETIRED_TEXT:
            return RETIRED_TEXT[self.resolution]
        legacy = StoredDecision(self.goal_id, self.resolution, self.generation)
        return DecidedResolution(legacy.carried(bus, orders))


@dataclass(frozen=True)
class StoredDecision(TypedRow):
    goal_id: GoalId
    legacy: str
    generation: int

    def carried(self, bus: Connection, orders: set[OrderId]) -> WorkDecision:
        if re.fullmatch("[0-9a-f]{32}", self.legacy):
            return RetryDecision(RetryDecisionId(self.legacy))
        if re.fullmatch("verified-turn:[0-9a-f]{32}", self.legacy):
            return VerifiedTurnDecision(TurnId(self.legacy.removeprefix("verified-turn:")))
        order_id = FieldCodec.decode(OrderId, self.legacy)
        answer = MessageIdentitySource.exists(bus, message_id=MessageId(self.legacy))
        order = order_id in orders
        if answer and order:
            raise ValueError(f"Both an order and a message own legacy decision: {self}")
        if answer:
            return ComplianceDecision(MessageId(self.legacy))
        if order:
            return OrderDecision(order_id)
        if self == KNOWN_ORPHAN:
            raise OrphanedDecisionError(self)
        raise ValueError(f"No message or order owns legacy decision: {self}")


# The one live decision no message or order owns; the owner chose to drop it.
KNOWN_ORPHAN = StoredDecision(GoalId("ba27e8c4c496427f8bda1094c9ed1c0c"), "c484f0fc45a6", 37)


class AttemptResolutions(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (GoalAttemptStore, TodoStore, Bus)

    def carry(self, *, apply: bool) -> Carried:
        (column,) = AttemptRecord._fields_named(("resolution",))
        (decision_column,) = GoalWorkDecision._fields_named(("decision",))
        counts: Counter[str] = Counter()
        rewritten: list[Path] = []
        changed = 0
        for path in self.input_files():
            with closing(GoalAttemptStore.read_snapshot(path)) as db:
                if not AttemptRecord.present(db):
                    continue
                legacy = tuple(
                    StoredResolution.read(
                        db.execute(
                            AttemptRecord._sql(
                                "SELECT {reservation} AS key,{resolution},"
                                "{goal_id},{generation} "
                                f'FROM "{AttemptRecord.declared_name}" '
                                "WHERE {resolution} IS NOT NULL AND CASE "
                                "WHEN json_valid({resolution}) "
                                "THEN json_type({resolution})!='object' "
                                "ELSE 1 END"
                            )
                        )
                    )
                )
                decisions: tuple[StoredDecision, ...] = ()
                schema_changes = 0
                if "decision_id" in GoalWorkDecision.stored_columns(db):
                    # An empty retired table still needs its declared schema carried.
                    schema_changes += 1
                    decisions = tuple(
                        StoredDecision.read(
                            db.execute(
                                f"SELECT goal_id,decision_id AS legacy,generation "
                                f'FROM "{GoalWorkDecision.declared_name}"'
                            )
                        )
                    )
            updates: list[tuple[object, object]] = []
            mapped: dict[str, str] = {}
            dropped: list[StoredDecision] = []
            if legacy or decisions:
                root = GoalAttemptStore.bus_root(path)
                orders: set[OrderId] = set()
                for todo_path in TodoStore.matching_in(TodoStore.directory_in(root)):
                    self.require_confined(todo_path)
                    with closing(TodoStore.read_snapshot(todo_path)) as todos:
                        orders.update(
                            order.id for todo in Todo.select(todos) for order in todo.order_history
                        )
                bus_path = Bus.path_in(Bus.directory_in(root))
                self.require_confined(bus_path)
                with closing(Bus.read_snapshot(bus_path)) as bus:
                    for stored in legacy:
                        try:
                            resolution = stored.carried(bus, orders)
                        except OrphanedDecisionError:
                            counts.update((f"orphan resolution {stored}",))
                            updates.append((None, stored.key))
                            continue
                        counts.update(("resolutions",))
                        updates.append((column.encode(resolution), stored.key))
                    for row in decisions:
                        try:
                            decision = row.carried(bus, orders)
                        except OrphanedDecisionError:
                            dropped.append(row)
                            counts.update(("decisions_dropped", f"dropped {row}"))
                            continue
                        counts.update(("decisions",))
                        mapped[row.legacy] = str(decision_column.encode(decision))
            counts += Counter(decision_schema=schema_changes)
            changed += len(updates) + len(decisions) + schema_changes
            if apply and (updates or schema_changes):
                with (
                    closing(GoalAttemptStore.writer(path)) as db,
                    GoalAttemptStore.write_transaction(db),
                ):
                    if updates:
                        self.rewrite_stored(db, AttemptRecord, column, updates)
                    if schema_changes:
                        for row in dropped:
                            GoalWorkDecision.delete(
                                db,
                                where="goal_id=? AND decision_id=?",
                                parameters=(row.goal_id, row.legacy),
                            )
                        db.create_function("carried_decision", 1, mapped.__getitem__)
                        GoalWorkDecision.rebuild(
                            db,
                            {"decision": 'carried_decision("decision_id")'},
                        )
                        db.create_function("carried_decision", 1, None)
                rewritten.append(path.relative_to(self.root))
        return Carried(changed, detail=dict(counts), rewritten=tuple(rewritten))
