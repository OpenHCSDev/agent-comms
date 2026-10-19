"""Flatten saved merged orders into their checked-order contract plus merge SHA.

Only this one-time carry understands the old original wrapper. Nested folded
orders are traversed too; production readers accept only the flat merged shape.
"""
from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

from agent_comms.coordination_database import CoordinationStore
from agent_comms.field_codec import FieldCodec
from agent_comms.todos import CheckedProblemOrder, MergedProblemOrder, Todo

from .carry import Carried, TodoOrderShape


class MergedOrders(TodoOrderShape):
    @classmethod
    def flattened(cls, order: dict) -> tuple[dict, int]:
        if "original" not in order:
            return order, 0
        original, changed = cls.flattened(order["original"])
        if order["kind"] != MergedProblemOrder.declared_name:
            return {**order, "original": original}, changed
        checked = {FieldCodec.wire_key(item) for item, _ in
                   FieldCodec.record_fields(CheckedProblemOrder)}
        flat = {key: value for key, value in original.items() if key in checked}
        flat.update(kind=MergedProblemOrder.declared_name, sha=order["sha"])
        # Validate the changed payload, not unrelated rows awaiting earlier
        # column/run carries. No old-shape decoder is retained in production.
        FieldCodec.decode(MergedProblemOrder, flat)
        return flat, changed + 1

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        path = cls.store.path_in(root)
        if not path.exists():
            return Carried(0)
        changed = 0
        with closing(CoordinationStore.writer(path)) as db:
            db.execute("BEGIN IMMEDIATE")
            for rowid, encoded in db.execute(f'SELECT rowid, "order" FROM "{Todo.declared_name}"'):
                order, count = cls.flattened(json.loads(encoded))
                if count:
                    db.execute(f'UPDATE "{Todo.declared_name}" SET "order"=? WHERE rowid=?',
                               (json.dumps(order), rowid))
                    changed += count
            db.commit() if apply else db.rollback()
        return Carried(changed, {"merged_orders": changed},
                       (path.name,) if apply and changed else ())


if __name__ == "__main__":
    MergedOrders.main()
