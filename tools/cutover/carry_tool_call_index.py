"""Install declared external indexes without changing any saved event."""

from pathlib import Path
from typing import ClassVar

from agent_comms.external_transcript import ExternalTranscript
from agent_comms.typed_table import SQLiteSchemaObject

from .carry import Carried, StoreCarry


class ToolCallIndex(StoreCarry):
    store = ExternalTranscript
    allowed_indexes: ClassVar[frozenset[str]] = frozenset(store.declared_schema()) - frozenset(
        row.declared_name for row in store.rows
    )

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        with cls.store_transaction(root, apply=apply) as db:
            if db is None:
                return Carried(0)
            changes = SQLiteSchemaObject.differences(
                cls.store._schema(db), cls.store.declared_schema()
            )
            for change in changes:
                if change.name not in cls.allowed_indexes or change.declared is None:
                    raise ValueError(
                        "External transcript differs outside its allowed declared indexes"
                    )
                if apply:
                    # Membership above admits only names validated by the declaration.
                    db.execute(f'DROP INDEX IF EXISTS "{change.name}"')
                    db.execute(change.declared)
        return cls.carried(root, len(changes), {"indexes": len(changes)}, apply=apply)


if __name__ == "__main__":
    ToolCallIndex.main()
