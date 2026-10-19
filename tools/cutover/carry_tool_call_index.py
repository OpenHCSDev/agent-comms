"""P679: add declared external tool-call indexes once, without changing any events."""
from contextlib import closing
from pathlib import Path

from agent_comms.coordination_database import CoordinationStore
from agent_comms.external_transcript import ExternalTranscript
from agent_comms.typed_table import SQLiteSchemaObject
from .carry import StoreCarry, Carried


class ToolCallIndex(StoreCarry):
    store = ExternalTranscript

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        path = cls.store.path_in(root)
        if not path.exists():
            return Carried(0)
        with closing(CoordinationStore.writer(path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            missing = SQLiteSchemaObject.differences(cls.store._schema(db), cls.store.declared_schema())
            for change in missing:
                if (change.stored is not None or change.declared is None
                        or not change.declared.startswith("CREATE INDEX ")):
                    raise ValueError("External transcript differs by more than missing declared indexes")
                if apply:
                    db.execute(change.declared)
        return Carried(len(missing), {"indexes": len(missing)}, (path.name,) if apply and missing else ())


if __name__ == "__main__":
    ToolCallIndex.main()
