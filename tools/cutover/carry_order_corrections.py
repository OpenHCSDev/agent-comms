"""Move stored check corrections into each order's one correction history.

The orders of a row are rewritten in one transaction, so a completed carry has no
check_corrections left to carry again. Delete this step after the live installation.
"""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import dataclass
from typing import ClassVar

from pydantic import JsonValue

from agent_comms.field_codec import FieldCodec
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceHolder
from agent_comms.registry_store import RegistryStore
from agent_comms.todos import CheckedProblemOrder, CorrectCheckTodo, Todo, TodoStore
from agent_comms.typed_table import TypedRow

RETIRED = "check_corrections"
HISTORY = FieldCodec.record_key(CheckedProblemOrder, "corrections")


def carried(wire: object) -> object:
    """The stored JSON with every order's check corrections moved into its history."""
    if isinstance(wire, list):
        return [carried(value) for value in wire]
    if not isinstance(wire, dict):
        return wire
    moved = {key: carried(value) for key, value in wire.items()}
    if RETIRED in moved:
        moved[HISTORY] = FieldCodec.encode(
            FieldCodec.decode(tuple[CorrectCheckTodo, ...], moved.pop(RETIRED))
        )
    return moved


@dataclass(frozen=True)
class StoredOrders(TypedRow):
    id: str
    order: dict[str, JsonValue]
    prior_orders: tuple[dict[str, JsonValue], ...]


class OrderCorrections(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (TodoStore,)

    def carry(self, *, apply: bool) -> Carried:
        path = TodoStore.path_in(self.root)
        self.require_confined(self.root, path)
        if not path.exists():
            return Carried(0)
        if apply:
            MaintenanceBarrier(RegistryStore.path_in(self.root)).read().require_held_by(
                MaintenanceHolder.current(), "Carry stored order check corrections"
            )
        marker = json.dumps(RETIRED)
        with TodoStore.observing(path) as db:
            rows = StoredOrders.read(
                db.execute(
                    f'SELECT id,"order",prior_orders FROM "{Todo.declared_name}" '
                    'WHERE instr("order",?) OR instr(prior_orders,?)',
                    (marker, marker),
                )
            )
        prepared = tuple(
            (row.id, carried(row.order), carried(list(row.prior_orders))) for row in rows
        )
        if apply and prepared:
            with closing(TodoStore.writer(path)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                for problem, order, prior_orders in prepared:
                    db.execute(
                        f'UPDATE "{Todo.declared_name}" SET "order"=?, prior_orders=? '
                        "WHERE id=?",
                        (json.dumps(order), json.dumps(prior_orders), problem),
                    )
        return Carried(
            len(prepared),
            detail={"todos": len(prepared)},
            rewritten=(path.relative_to(self.root),) if apply and prepared else (),
        )
