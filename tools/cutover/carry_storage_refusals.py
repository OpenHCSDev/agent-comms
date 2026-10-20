"""Keep old storage strikes on the board, but void every one still counting."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import cast

from agent_comms.field_codec import FieldCodec
from agent_comms.todos import StrikeTodo, Todo, TodoStore, VoidStrikeTodo

from .carry import Carried, TodoRows


class StorageRefusals(TodoRows):
    old_kind = "storage_strike_todo"
    reason = "storage refusals refuse the command and never strike"

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        if not cls.store.path_in(root).exists():
            return Carried(0)
        changed = voids = 0
        with cls.todo_rows(root, apply=apply) as connection:
            db = cast(sqlite3.Connection, connection)
            table = Todo.declared_name
            plain_kind = FieldCodec.encode(StrikeTodo, type[StrikeTodo])
            names = db.execute(
                f'SELECT id FROM "{table}" WHERE '
                "EXISTS(SELECT 1 FROM json_tree(strikes) WHERE key='kind' AND value=?) OR "
                "EXISTS(SELECT 1 FROM json_tree(voided) WHERE key='kind' AND value=?)",
                (cls.old_kind, cls.old_kind),
            ).fetchall()
            for (identity,) in names:
                indices = db.execute(
                    f'SELECT j.key FROM "{table}" t,json_each(t.strikes) j '
                    "WHERE t.id=? AND json_extract(j.value,'$.kind')=?",
                    (identity, cls.old_kind),
                ).fetchall()
                db.execute(
                    f'UPDATE "{table}" SET strikes=(SELECT json_group_array(json(part)) FROM '
                    "(SELECT CASE WHEN json_extract(value,'$.kind')=? "
                    "THEN json_set(value,'$.kind',?) ELSE value END AS part "
                    "FROM json_each(strikes) ORDER BY key)),"
                    "voided=(SELECT json_group_array(json(part)) FROM "
                    "(SELECT CASE WHEN json_extract(value,'$.strike.kind')=? "
                    "THEN json_set(value,'$.strike.kind',?) ELSE value END AS part "
                    "FROM json_each(voided) ORDER BY key)) WHERE id=?",
                    (
                        cls.old_kind,
                        plain_kind,
                        cls.old_kind,
                        plain_kind,
                        identity,
                    ),
                )
                current = TodoStore._current(db, identity)
                for (index,) in indices:
                    strike = current.strikes[index]
                    if strike in current.live_strikes():
                        # A carry records the owner's exact eight-word reason;
                        # it does not replay a command with a ten-word minimum.
                        taken_back = VoidStrikeTodo(
                            current.revision, current.creator_identity, strike, cls.reason
                        )
                        current = replace(
                            current,
                            voided=(*current.voided, taken_back),
                            revision=current.revision + 1,
                        )
                        voids += 1
                current.upsert(db)
                changed += len(indices)
        return cls.carried(root, changed, {"strikes": changed, "voids": voids}, apply=apply)


if __name__ == "__main__":
    StorageRefusals.main()
