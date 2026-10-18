#!/usr/bin/env python3
"""Stopped-runtime carry: add the continuation to existing external item rows.

Run once for each Core root before starting the new runtime. No saved items or
positions are changed. Production readers accept only the new declared schema.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from pathlib import Path

from agent_comms.coordination_database import CoordinationStore
from agent_comms.external_transcript import ExternalItemRow, ExternalTranscript, NoEarlierPage


def carry(root: Path) -> int:
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
                db.rollback()
                return 0
            original = tuple(item.wire_name for item in fields if item.name != 'earlier')
            if actual != original:
                raise ValueError(f'Unexpected external item columns: {actual!r}')
            saved = f'{table}_before_pages'
            db.execute(f'ALTER TABLE "{table}" RENAME TO "{saved}"')
            ExternalItemRow.create(db)
            continuation, = (item for item in fields if item.name == 'earlier')
            columns = ExternalItemRow._column_list(expected)
            selected = ', '.join('?' if item.name == 'earlier' else f'"{item.wire_name}"'
                                 for item in fields)
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
    print(carry(parser.parse_args().root))
