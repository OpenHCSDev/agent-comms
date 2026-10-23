"""Retire the stored direct-reply answer before reconciling the message-owned policy.

Delete this step after every bus has run it. The exact input declaration comes
from DirectReplySource/ParticipantSource at 477a44afb; it is never part of the
incoming BusProjectionTables family.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.bus import Bus
from agent_comms.bus_tables import BusSchema
from agent_comms.historical_views import HistoryArchive
from agent_comms.installer.carry import Carried, Carry
from agent_comms.locked_store import SQLiteFile
from agent_comms.store_files import _store_lock
from agent_comms.typed_table import (
    Column,
    DerivedTables,
    SharedFileTables,
    SQLiteTableName,
    TypedTable,
)
from .carry_bus_history import CarryCatalogSource


@dataclass(frozen=True)
class StoredDirectReply(TypedTable, SharedFileTables, DerivedTables, declared_name="direct_reply_source"):
    """Only the removed input shape; no incoming schema family owns this row."""

    lookup: Annotated[str, Column(primary_key=True)]
    seq: Annotated[int, Column(primary_key=True)]


class ReplyPolicy(Carry):
    def backup_files(self) -> tuple[Path, ...]:
        sources = CarryCatalogSource.read(self.root, self.original_root)
        roots = (self.root, *(source.mapped_root(self.root, self.original_root) for source in sources))
        paths = tuple(dict.fromkeys(Bus.path_in(root) for root in roots))
        for path in paths:
            self.require_confined(self.root, path)
        return tuple(path for path in paths if path.exists())

    def carry(self, *, apply: bool) -> Carried:
        changed = rows = 0
        rewritten = []
        with _store_lock(HistoryArchive.path_in(self.root)):
            for path in self.backup_files():
                with closing(SQLiteFile.read_snapshot(path)) as db:
                    present = StoredDirectReply.present(db)
                    if not present and not BusSchema.schema_differs(db):
                        continue
                    if present:
                        StoredDirectReply.require_schema(db)
                        rows += StoredDirectReply.count(db)
                if apply:
                    # The installer's main-root hold also owns its retained sources.
                    Bus.schema_change(Bus.path_in(self.root)).require()
                    with closing(SQLiteFile.writer(path, isolation_level=None)) as db:
                        with SQLiteFile.write_transaction(db):
                            if present:
                                StoredDirectReply.require_schema(db)
                                SQLiteTableName(StoredDirectReply.declared_name).drop(db)
                            # Catalog snapshots use the same current family carry as main.
                            BusSchema.carry_schema(db)
                changed += 1
                rewritten.append(str(path.relative_to(self.root)))
        return Carried(changed, {"retired_direct_replies": rows}, tuple(rewritten)) if changed else Carried(0)
