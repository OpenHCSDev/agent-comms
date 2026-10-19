"""Storage refusals keep their historical strike receipt, but never a live strike."""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import JsonValue

from agent_comms.field_codec import FieldCodec
from agent_comms.todos import StrikeTodo, Todo, VoidStrikeTodo

from .carry import Carried, TodoRows


class StorageRefusals(TodoRows):
    old_kind = "storage_strike_todo"
    reason = "storage refusals refuse the command and never strike"

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        changed = voids = 0
        with cls.todo_rows(root, apply=apply) as db:
            if db is None:
                return Carried(0)
            for identity, encoded, receipts in db.execute(
                f'SELECT id,strikes,voided FROM "{Todo.declared_name}"'
            ).fetchall():
                strikes = FieldCodec.decode(list[dict[str, JsonValue]], FieldCodec.parse(encoded))
                recorded = FieldCodec.decode(list[dict[str, JsonValue]], FieldCodec.parse(receipts))
                storage = [strike for strike in strikes if strike.get("kind") == cls.old_kind]
                nested = [FieldCodec.decode(dict[str, JsonValue], receipt["strike"])
                          for receipt in recorded]
                historical = [strike for strike in nested if strike.get("kind") == cls.old_kind]
                if not storage and not historical:
                    continue
                for strike in (*storage, *historical):
                    strike["kind"] = StrikeTodo.declared_name
                for receipt, strike in zip(recorded, nested):
                    receipt["strike"] = strike
                db.execute(f'UPDATE "{Todo.declared_name}" SET strikes=?,voided=? WHERE id=?',
                           (json.dumps(strikes), json.dumps(recorded), identity))
                current = Todo.one(db, id=identity)
                if current is None:
                    raise ValueError(f"Todo disappeared during storage-strike carry: {identity}")
                for original in storage:
                    strike = FieldCodec.decode(StrikeTodo, original)
                    if strike in current.live_strikes():
                        current = VoidStrikeTodo(current.revision, current.creator_identity,
                                                 strike, cls.reason).apply(current)
                        voids += 1
                current.upsert(db)
                changed += len(storage)
        return cls.carried(root, changed, {"strikes": changed, "voids": voids}, apply=apply)


if __name__ == "__main__":
    StorageRefusals.main()
