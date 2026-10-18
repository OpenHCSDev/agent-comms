#!/usr/bin/env python3
"""Stopped-runtime carry: add the continuation to existing external item rows.

Run once for each Core root before starting the new runtime. No saved items or
positions are changed. Production readers accept only the new declared schema.
"""
from __future__ import annotations

import argparse
import json
from contextlib import closing
from pathlib import Path

from agent_comms.coordination_database import CoordinationStore
from agent_comms.external_transcript import ExternalItemRow, ExternalTranscript, NoEarlierPage


def carry(root: Path, *, apply: bool = True) -> int:
    path = ExternalTranscript.path_in(root)
    if not path.exists():
        return 0
    with closing(CoordinationStore.writer(path)) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            table = ExternalItemRow.declared_name
            fields = ExternalItemRow._fields()
            actual = tuple(row[1] for row in db.execute(f'PRAGMA table_info("{table}")'))
            expected = tuple(item.wire_name for item in fields)
            if actual == expected:
                for row in ExternalItemRow.stream(db):
                    pass
                db.rollback()
                return 0
            original = tuple(item.wire_name for item in fields if item.name != 'earlier')
            if actual != original:
                raise ValueError(f'Unexpected external item columns: {actual!r}')
            continuation, = (item for item in fields if item.name == 'earlier')
            selected = ', '.join(f'? AS "{item.wire_name}"' if item.name == 'earlier'
                                 else f'"{item.wire_name}"' for item in fields)
            count = sum(1 for row in ExternalItemRow.iterate(db.execute(
                f'SELECT {selected} FROM "{table}"', (continuation.encode(NoEarlierPage()),))))
            if not apply:
                db.rollback()
                return count
            saved = f'{table}_before_pages'
            db.execute(f'ALTER TABLE "{table}" RENAME TO "{saved}"')
            ExternalItemRow.create(db)
            columns = ExternalItemRow._column_list(expected)
            changed = db.execute(f'INSERT INTO "{table}" ({columns}) SELECT {selected} FROM "{saved}"',
                                 (continuation.encode(NoEarlierPage()),)).rowcount
            db.execute(f'DROP TABLE "{saved}"')
            db.commit()
            return changed
        except BaseException:
            db.rollback()
            raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    count = carry(args.root, apply=args.apply)
    print(json.dumps({'rows': count, 'decode_failures': [], 'applied': args.apply}))
