"""One-time cutover: publication intents hold their Message instead of copied fields.

Schema 10 stored each frozen response as sender, type, notice, timestamp,
payload and digest columns. Schema 11 stores the Message itself. This rebuilds
publication_intents with the declared DDL, encoding each row's Message and
checking it reproduces the stored expected message ID, then installs the
schema 11 metadata. Run it with the fleet stopped and every Toad closed; the
database is copied into BACKUP_DIR first.

Usage: publication_message.py ROOT BACKUP_DIR
"""

import shutil
import sqlite3
import sys
from pathlib import Path

from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_schema import COORDINATION_SCHEMA_VERSION
from agent_comms.coordination_tables.metadata import SchemaMeta
from agent_comms.coordination_tables.publications import PublicationIntents
from agent_comms.messages import Message, MessageKind
from agent_comms.typed_table import JsonStorage


def carried_rows(db: sqlite3.Connection):
    for row in db.execute(
        "SELECT execution_id, sender, exact_target, message_type, notice, timestamp,"
        " payload, publication_key, expected_message_id FROM publication_intents"
    ):
        execution_id, sender, target, kind, notice, timestamp, payload, key, expected = row
        message = Message(
            sender, target, payload, MessageKind.decode(kind),
            timestamp=float(timestamp), notice=bool(notice),
        )
        if message.message_id != expected:
            raise SystemExit(f"{execution_id}/{target}: Message does not reproduce {expected}")
        yield execution_id, target, JsonStorage.to_sql(message), key, expected


def migrate(path: Path) -> int:
    db = sqlite3.connect(path, isolation_level=None)
    try:
        if db.execute("PRAGMA user_version").fetchone()[0] != COORDINATION_SCHEMA_VERSION - 1:
            raise SystemExit("coordination schema is not the version this carry starts from")
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("PRAGMA legacy_alter_table=ON")
        db.execute("BEGIN EXCLUSIVE")
        rows = tuple(carried_rows(db))
        objects = PublicationIntents.schema_objects()
        table = objects.pop(PublicationIntents.declared_name)
        db.execute(table.replace('CREATE TABLE "publication_intents"',
                                 'CREATE TABLE "publication_intents_carry"', 1))
        db.executemany("INSERT INTO publication_intents_carry VALUES (?,?,?,?,?)", rows)
        db.execute("DROP TABLE publication_intents")
        db.execute("ALTER TABLE publication_intents_carry RENAME TO publication_intents")
        for statement in objects.values():
            db.execute(statement)
        meta = SchemaMeta.schema_objects()
        db.execute("DROP TRIGGER schema_meta_update_frozen")
        db.execute("UPDATE schema_meta SET schema_version=? WHERE singleton=1",
                   (COORDINATION_SCHEMA_VERSION,))
        db.execute(meta["schema_meta_update_frozen"])
        db.execute(f"PRAGMA user_version = {COORDINATION_SCHEMA_VERSION}")
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise SystemExit("foreign keys broken by the carry")
        db.execute("COMMIT")
        return len(rows)
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    root, backup = Path(sys.argv[1]), Path(sys.argv[2])
    path = root / "coordination.sqlite3"
    backup.mkdir(parents=True, exist_ok=False, mode=0o700)
    shutil.copy2(path, backup / path.name)
    carried = migrate(path)
    with CoordinationStore(path) as store:
        stored = tuple(PublicationIntents.select(store._connection))
    print("carried", carried, "intents; read back", len(stored), "; backup", backup)
