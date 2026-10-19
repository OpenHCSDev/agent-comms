"""One offline carry of committed byte locations into existing runtime rows.

The installer stops bus owners and backs up coordination.sqlite3. Native
sessions and shared proof journals are read only; no input is replayed.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from agent_comms.coordinated_runtime_schema import _schema, _digest
from agent_comms.coordination_database import CoordinationStore
from agent_comms.native_entries import NativeEntry, SessionEntry
from agent_comms.native_pi import PrivateEvidenceRead
from agent_comms.native_runtime_input import NativeRuntimeInput, NativeRuntimeSchemaMeta
from agent_comms.native_transcript import NativeTranscript
from cutover.carry import Carry, Carried


def input_offsets(session: Path) -> dict[str, tuple[str, int]]:
    """The existing forward reader supplies positions from a verified source cut."""
    positions = {}
    with PrivateEvidenceRead.open(session) as source:
        if not isinstance(NativeEntry.from_wire(source.header()), SessionEntry):
            raise ValueError('Native offset carry requires a session header')
        for record in NativeTranscript(session).forward(0, source.size):
            if not record.complete:
                raise ValueError('Native offset carry requires a complete committed cut')
            user = record.entry.tracked_user
            if user is not None:
                if user.input_id in positions:
                    raise ValueError('Native offset carry found duplicate committed input IDs')
                positions[user.input_id] = (record.entry.id, record.start)
        source.verify()
    return positions


def rewrite_table(db, table, rows):
    db.execute(f'DROP TABLE "{table.declared_name}"')
    for sql in table.schema_objects().values():
        db.execute(sql)
    for row in rows:
        row.insert(db)


class NativeInputOffsets(Carry):
    @classmethod
    def backup_files(cls, root):
        return (root / 'coordination.sqlite3',)

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        path = root / 'coordination.sqlite3'
        if not path.exists():
            return Carried(0)
        with closing(CoordinationStore.read_snapshot(path)) as db:
            version, digest = db.execute('SELECT version,ddl_digest FROM native_runtime_schema_meta').fetchone()
            if version == 7:
                from agent_comms.coordinated_runtime_schema import assert_native_runtime_schema
                assert_native_runtime_schema(db)
                return Carried(0)
            if version != 6:
                raise ValueError('Native offset carry requires runtime schema6')
            declared = _schema()
            actual = dict(db.execute('SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL'))
            old = {name: actual[name] for name in declared}
            if _digest(old) != digest:
                raise ValueError('Native offset carry refuses a changed original runtime schema')
            locations = tuple(db.execute('SELECT input_id,session_file,session_entry_id FROM native_runtime_input'))
            positions = {Path(file): input_offsets(Path(file))
                         for file in {file for _id, file, entry in locations if entry is not None}}
            rows = []
            for input_id, file, entry_id in locations:
                start = None
                if entry_id is not None:
                    native_id, start = positions[Path(file)][input_id]
                    if native_id != entry_id:
                        raise ValueError('Native offset carry row names another original entry')
                columns = ','.join('? AS session_entry_start' if name == 'session_entry_start'
                                   else f'"{wire}"' for name, wire in NativeRuntimeInput.column_projection().items())
                rows.extend(NativeRuntimeInput.read(db.execute(
                    f'SELECT {columns} FROM native_runtime_input WHERE input_id=?', (start, input_id))))
        if apply:
            with closing(sqlite3.connect(path, timeout=0)) as db, db:
                db.execute('PRAGMA foreign_keys=OFF')
                db.execute('BEGIN EXCLUSIVE')
                rewrite_table(db, NativeRuntimeInput, rows)
                rewrite_table(db, NativeRuntimeSchemaMeta,
                              [NativeRuntimeSchemaMeta(singleton=1, ddl_digest=_digest(_schema()))])
                if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
                    raise ValueError('Native offset carry violates original foreign keys')
        return Carried(len(rows), rewritten=('coordination.sqlite3',))


if __name__ == '__main__':
    NativeInputOffsets.main()
