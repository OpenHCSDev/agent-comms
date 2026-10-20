"""Carry retired merged wrappers through the checked order's own transition."""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from agent_comms.coordination_database import CoordinationStore
from agent_comms.field_codec import FieldCodec
from agent_comms.todos import CheckedProblemOrder, MergedProblemOrder, Todo, TodoStore
from agent_comms.typed_table import TypedRow

from agent_comms.installer.carry import Carried, StoreCarry


@dataclass(frozen=True)
class SavedMergedOrder(TypedRow):
    """The old wrapper projected and decoded once at the SQLite boundary."""

    id: str
    path: str
    checked: CheckedProblemOrder
    sha: str


class MergedOrders(StoreCarry):
    store = TodoStore

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        path = cls.store.path_in(root)
        if not path.exists():
            return Carried(0)
        parameters = (MergedProblemOrder.declared_name,)
        with closing(CoordinationStore.writer(path)) as db:
            db.execute("BEGIN IMMEDIATE")
            # Folded provenance can contain a merged order too. The transition
            # belongs to CheckedProblemOrder, not a second field list here.
            saved = SavedMergedOrder.read(
                db.execute(
                    f"SELECT t.id, n.path AS path,"
                    f" json_extract(t.\"order\", n.path || '.original') AS checked,"
                    f" json_extract(t.\"order\", n.path || '.sha') AS sha"
                    f' FROM "{Todo.declared_name}" t, json_tree(t."order") n'
                    " WHERE n.key='kind' AND n.value=?"
                    " AND json_type(t.\"order\", n.path || '.original')='object'"
                    " ORDER BY length(n.path) DESC",
                    parameters,
                )
            )
            for row in saved:
                merged = row.checked.merged(row.sha)
                db.execute(
                    f'UPDATE "{Todo.declared_name}"'
                    ' SET "order"=json_set("order", ?, json(?)) WHERE id=?',
                    (row.path, json.dumps(FieldCodec.encode(merged)), row.id),
                )
            db.commit() if apply else db.rollback()
        return Carried(
            len(saved), {"merged_orders": len(saved)}, (path.name,) if apply and saved else ()
        )
