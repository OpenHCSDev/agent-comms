"""P717: reconcile the event declaration's indexes without rewriting events."""

from contextlib import closing
from pathlib import Path

from agent_comms.coordination_database import CoordinationStore
from agent_comms.external_transcript import ExternalEventRow, ExternalTranscript
from agent_comms.typed_table import SQLiteSchemaObject, _identifier
from .carry import Carried, StoreCarry


class ExternalEventIndexes(StoreCarry):
    store = ExternalTranscript

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        path = cls.store.path_in(root)
        if not path.exists():
            return Carried(0)
        objects = ExternalEventRow.schema_objects()
        indexes = objects.keys() - {ExternalEventRow.declared_name}
        with closing(CoordinationStore.writer(path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            changes = SQLiteSchemaObject.differences(
                cls.store._schema(db), cls.store.declared_schema()
            )
            if any(change.name not in indexes for change in changes):
                raise ValueError("External transcript differs outside its declared event indexes")
            if apply:
                for change in changes:
                    if change.stored is not None:
                        db.execute(f"DROP INDEX {_identifier(change.name)}")
                    db.execute(objects[change.name])
        return Carried(
            len(changes), {"indexes": len(changes)}, (path.name,) if apply and changes else ()
        )


if __name__ == "__main__":
    ExternalEventIndexes.main()
