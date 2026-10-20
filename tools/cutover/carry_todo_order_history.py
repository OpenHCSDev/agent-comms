"""Give the Todo store its prior declarations without exposing them as order inputs."""

from contextlib import closing
from pathlib import Path

from agent_comms.coordination_database import CoordinationStore
from agent_comms.field_codec import FieldCodec
from agent_comms.todos import Todo

from .carry import Carried, Carry, TodoRows


class TodoOrderHistory(TodoRows):
    @classmethod
    def precedes(cls, later: type[Carry]) -> bool:
        return issubclass(later, TodoRows) and later is not cls

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        path = cls.store.path_in(root)
        if not path.exists():
            return Carried(0)
        archive = Todo._fields_named(("prior_orders",))[0]
        with closing(CoordinationStore.writer(path)) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                cursor = db.execute(f'SELECT * FROM "{Todo.declared_name}"')
                description = cursor.description
                if not description:
                    raise ValueError("The todo store did not return declared columns")
                columns = tuple(column[0] for column in description)
                if archive.wire_name in columns:
                    cls.store(root).require_schema(cls.store._schema(db))
                    return Carried(0)
                if set(columns) | {archive.wire_name} != set(Todo.columns()):
                    raise ValueError("The todo store has an unexpected historical schema")
                positions = {name: index for index, name in enumerate(columns)}
                saved = tuple(
                    FieldCodec.construct(
                        Todo,
                        {
                            item.name: item.decode(row[positions[item.wire_name]])
                            for item in Todo._fields()
                            if item.wire_name in positions
                        },
                    )
                    for row in cursor
                )
                if apply:
                    Todo.drop(db)
                    Todo.create(db)
                    for todo in saved:
                        todo.upsert(db)
                    cls.store(root).require_schema(cls.store._schema(db))
                    db.commit()
                return cls.carried(root, len(saved) + 1, {"rows": len(saved)}, apply=apply)
            finally:
                if db.in_transaction:
                    db.rollback()


if __name__ == "__main__":
    TodoOrderHistory.main()
