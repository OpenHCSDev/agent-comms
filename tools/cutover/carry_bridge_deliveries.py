"""Carry bridge duplicate suppression once, with the old watcher stopped.

Run with the new Core. The backup keeps the complete old journal, including
partial Monitor observations. No outcome is promoted and no Core read is made.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

from agent_comms.cli_bridges.shared import Binding, DeliveryRow, ObservedPart
from agent_comms.cli_protocol import Outcome
from agent_comms.coordination_database import CoordinationStore
from agent_comms.field_codec import FieldCodec


def carry(state: Path, backup: Path) -> dict:
    journal = state.resolve() / 'delivery.sqlite3'
    with closing(sqlite3.connect(journal.as_uri() + '?mode=rw', uri=True)) as db:
        binding, = Binding.select(db)
        locks = Path.home() / '.local/state/agent-comms/cli-watch'
        locks.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = hashlib.sha256(binding.thread.encode()).hexdigest()
        with (locks / f'{key}.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            db.execute('PRAGMA synchronous=FULL')
            with db:
                db.execute('BEGIN IMMEDIATE')
                if Binding.select(db) != [binding]:
                    raise ValueError('Bridge binding changed before the carry')
                columns = tuple(row[1] for row in db.execute('PRAGMA table_info(delivery)'))
                if columns != ('id', 'payload', 'outcome'):
                    raise ValueError(f'Expected old delivery schema; found {columns}')
                tables = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if tables - {'binding', 'delivery', 'monitor_attempt', 'monitor_part'}:
                    raise ValueError(f'Unexpected bridge tables: {sorted(tables)}')
                rows = [DeliveryRow(mid, payload, FieldCodec.decode(type[Outcome], outcome))
                        for mid, payload, outcome in db.execute(
                            'SELECT id, payload, outcome FROM delivery ORDER BY id')]
                # Retain completed IDs too: a crash between journal completion
                # and Core acknowledgement must not turn them into new sends.
                for row in rows:
                    if row.message.message_id != row.id:
                        raise ValueError(f'Message ID disagrees with journal key: {row.id}')
                backup = backup.resolve()
                backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.close(os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
                # Read through another connection while the writer lock prevents
                # changes. SQLite backup includes committed WAL contents.
                with closing(CoordinationStore.read_snapshot(journal)) as source:
                    with closing(sqlite3.connect(backup)) as saved:
                        source.backup(saved)
                db.execute('DROP TABLE IF EXISTS monitor_part')
                db.execute('DROP TABLE IF EXISTS monitor_attempt')
                db.execute('DROP TABLE delivery')
                DeliveryRow.create(db)
                ObservedPart.create(db)
                for row in rows:
                    row.insert(db)
                if DeliveryRow.select(db, order_by=('id',)) != rows:
                    raise ValueError('Typed delivery rows differ after carry')
                if Binding.select(db) != [binding]:
                    raise ValueError('Bridge binding changed during carry')
            return {'journal': str(journal), 'backup': str(backup),
                    'rows': len(rows),
                    'outcomes': dict(Counter(row.outcome.declared_name for row in rows))}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--backup', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(carry(args.state, args.backup), indent=2))
