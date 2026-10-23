"""Run the real bus against one copied message producer in a fresh interpreter."""

from __future__ import annotations

import importlib.abc
import importlib.util
import json
from pathlib import Path
import sys


class MessageProducerSource(importlib.abc.MetaPathFinder):
    module_name = "agent_comms.messages"

    def __init__(self, source: Path):
        self.source = source

    def find_spec(self, fullname, path, target=None):
        if fullname == self.module_name:
            return importlib.util.spec_from_file_location(fullname, self.source)


def main(source: Path, database: Path, after_import_source: Path | None = None) -> None:
    sys.dont_write_bytecode = True
    sys.meta_path.insert(0, MessageProducerSource(source))

    from agent_comms.bus import Bus, PublicRecord
    from agent_comms.bus_tables import BusRow, BusSchema, MessageIdentitySource
    from agent_comms.cli_commands import PlannedReconcileCliCommand
    from agent_comms.messages import InfoKind, Message
    from agent_comms.typed_table import ProjectionProducer, WriterOwnedFile

    if after_import_source is not None:
        source.write_bytes(after_import_source.read_bytes())
    created = not database.exists()
    bus = Bus.install(database) if created else Bus(database)
    if created:
        bus.append(lambda seq, db: PublicRecord.from_message(
            Message("author", "worker", "same durable source", InfoKind, seq=seq),
        ))
    planned = PlannedReconcileCliCommand.changes(Bus, database)
    with bus.writer_connection() as db:
        before = db.total_changes
        changed = BusSchema.reconcile_schema(db, WriterOwnedFile())
        projected = MessageIdentitySource.require(db, seq=1)
        print(json.dumps({
            "created": created,
            "reconciled": changed,
            "planned": planned,
            "row_writes": db.total_changes - before,
            "ddl": BusSchema.declared_schema(),
            "source_rows": BusRow.count(db),
            "source": bus.lookup(1).message.to_wire(),
            "policy": projected.response_policy.__name__,
            "producer": ProjectionProducer.require(
                db, projection=MessageIdentitySource.declared_name).identity,
            "fresh": not BusSchema.schema_differs(db),
            "integrity": db.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_keys": db.execute("PRAGMA foreign_key_check").fetchall(),
        }))


if __name__ == "__main__":
    main(*(Path(argument) for argument in sys.argv[1:]))
