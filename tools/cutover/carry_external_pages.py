"""External item rows gain their earlier-page continuation (P427).

This step owns its historical SQL shape. The current transcript declaration
has since moved events to separate rows; using it here would drop old events
before the later position conversion can read them.
"""
from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

from agent_comms.coordination_database import CoordinationStore
from agent_comms.external_transcript import ExternalEventRow, ExternalTranscript, NoEarlierPage
from agent_comms.field_codec import FieldCodec

from agent_comms.installer.carry import Carried, Carry


def carry(root: Path, *, apply: bool) -> int:
    path = ExternalTranscript.path_in(root)
    if not path.exists():
        return 0
    with closing(CoordinationStore.writer(path)) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            table = 'external_item'
            if db.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?',
                          ('table', ExternalEventRow.declared_name)).fetchone():
                db.rollback()
                return 0  # A later step already replaced this historical shape.
            actual = tuple(row[1] for row in db.execute(f'PRAGMA table_info("{table}")'))
            expected = ('thread', 'turn_id', 'item_id', 'seq', 'earlier', 'events')
            if actual == expected:
                db.rollback()
                return 0
            original = tuple(column for column in expected if column != 'earlier')
            if actual != original:
                raise ValueError(f'Unexpected external item columns: {actual!r}')
            count, = db.execute(f'SELECT count(*) FROM "{table}"').fetchone()
            if not apply:
                db.rollback()
                return count
            saved = f'{table}_before_pages'
            db.execute(f'ALTER TABLE "{table}" RENAME TO "{saved}"')
            db.execute(f'''CREATE TABLE "{table}" (
                thread TEXT NOT NULL, turn_id TEXT NOT NULL, item_id TEXT NOT NULL,
                seq INTEGER NOT NULL, earlier TEXT NOT NULL, events TEXT NOT NULL,
                PRIMARY KEY (thread,turn_id,item_id), UNIQUE (thread,seq)) STRICT''')
            continuation = json.dumps(FieldCodec.encode(NoEarlierPage()), separators=(',', ':'))
            db.execute(f'INSERT INTO "{table}" SELECT thread,turn_id,item_id,seq,?,events '
                       f'FROM "{saved}" ORDER BY rowid', (continuation,))
            db.execute(f'DROP TABLE "{saved}"')
            db.commit()
            return count
        except BaseException:
            db.rollback()
            raise


class ExternalPages(Carry):
    """External item rows gain the continuation to their earlier page."""


    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        changed = carry(root, apply=apply)
        rewritten = tuple(path.name for path in root.glob(f'{ExternalTranscript.path_in(root).name}*'))
        return Carried(changed, {}, rewritten if changed and apply else ())
