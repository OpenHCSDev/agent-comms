"""Rebuild the context request table on its incarnation key.

Delete after every bus has run this step. The retired name column and its
index are generated from the stored identity, so the rebuild keeps every row
and its id; the declared table owns the new key.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from agent_comms.context_store import ContextRequest, ContextStore
from agent_comms.installer.carry import Carried, Carry
from agent_comms.locked_store import SQLiteFile
from agent_comms.store_files import _store_lock
from agent_comms.typed_table import SQLiteSchemaObject, TypedTable


class ContextRequestKey(Carry):
    opened_files = (ContextStore,)
    @property
    def path(self) -> Path:
        return self.root / ContextStore.filename


    def carry(self, *, apply: bool) -> Carried:
        if not self.path.exists():
            return Carried(0)
        with _store_lock(self.path), closing(
            ContextStore.read_snapshot(self.path)
        ) as db:
            if not SQLiteSchemaObject.differences(
                TypedTable.objects_of(db, (ContextRequest,)), ContextRequest.schema_objects()
            ):
                return Carried(0)
            (rows,) = db.execute(f'SELECT count(*) FROM "{ContextRequest.declared_name}"').fetchone()
            if not apply:
                return Carried(1, {"context_requests": rows})
        with _store_lock(self.path), closing(ContextStore.writer(self.path, isolation_level=None)) as db:
            ContextStore.schema_change(self.path).require()
            with SQLiteFile.write_transaction(db):
                ContextRequest.rebuild(db)
                ContextStore.require_schema(db)
            return Carried(1, {"context_requests": rows}, (self.path.relative_to(self.root),))
