"""Todo rows gain the strikes voided on them (P47).

No saved strike, order or assignment is changed. Production readers accept
only the new declared schema.
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path

from agent_comms.coordination_database import CoordinationStore
from agent_comms.todos import Todo, TodoStore

from cutover.carry import Carried, Carry


def carry(root: Path, *, apply: bool) -> int:
    path = TodoStore.path_in(root)
    if not path.exists():
        return 0
    with closing(CoordinationStore.writer(path)) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            table = Todo.declared_name
            fields = Todo._fields()
            actual = tuple(row[1] for row in db.execute(f'PRAGMA table_info("{table}")'))
            expected = tuple(item.wire_name for item in fields)
            if actual == expected:
                for row in Todo.stream(db):
                    pass
                db.rollback()
                return 0
            original = tuple(item.wire_name for item in fields if item.name != 'voided')
            if actual != original:
                raise ValueError(f'Unexpected todo columns: {actual!r}')
            voided, = (item for item in fields if item.name == 'voided')
            selected = ', '.join(f'? AS "{item.wire_name}"' if item.name == 'voided'
                                 else f'"{item.wire_name}"' for item in fields)
            count = sum(1 for row in Todo.iterate(db.execute(
                f'SELECT {selected} FROM "{table}"', (voided.encode(()),))))
            if not apply:
                db.rollback()
                return count
            saved = f'{table}_before_voids'
            db.execute(f'ALTER TABLE "{table}" RENAME TO "{saved}"')
            Todo.create(db)
            columns = Todo._column_list(expected)
            changed = db.execute(
                f'INSERT INTO "{table}" ({columns}) SELECT {selected} FROM "{saved}"',
                (voided.encode(()),)).rowcount
            db.execute(f'DROP TABLE "{saved}"')
            db.commit()
            return changed
        except BaseException:
            db.rollback()
            raise


class TodoVoids(Carry):
    """Todo rows gain the strikes voided on them; every stored strike stays as it was."""

    after = 'carry_display_counts'

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        changed = carry(root, apply=apply)
        rewritten = tuple(path.name for path in root.glob(f'{TodoStore.path_in(root).name}*'))
        return Carried(changed, {}, rewritten if changed and apply else ())


if __name__ == '__main__':
    TodoVoids.main()
