"""One-time journal constraint migration; run at install, then delete this tool."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_states import SummaryState


def migrate(path: Path, template: Path) -> dict:
    """Take schema from its current owner; retain every original row verbatim."""
    if template.exists() or template == path:
        raise ValueError("A new owned schema-template path is required")
    CompactionJournal(template)
    table = 'selected_summary_attempts'
    try:
        with sqlite3.connect(template) as current:
            ddl = current.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()[0]
            columns = current.execute(f'PRAGMA table_info({table})').fetchall()
            indices = current.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? "
                "AND sql IS NOT NULL", (table,)
            ).fetchall()
        with sqlite3.connect(path) as db:
            db.execute('PRAGMA synchronous=EXTRA')
            db.execute('BEGIN IMMEDIATE')
            if db.execute(f'PRAGMA table_info({table})').fetchall() != columns:
                raise ValueError('Journal columns differ; no migration was applied')
            states = db.execute(
                f'SELECT status,commit_id,decline_reason FROM {table}'
            )
            count = 0
            for state in states:
                SummaryState.from_columns(*state)
                count += 1
            old_ddl = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()[0]
            if old_ddl == ddl:
                return {'rows': count, 'changed': False}
            old_indices = db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? "
                "AND sql IS NOT NULL", (table,)
            ).fetchall()
            for (name,) in old_indices:
                db.execute('DROP INDEX "' + name.replace('"', '""') + '"')
            prior = '_before_summary_state_cutover'
            db.execute(f'ALTER TABLE {table} RENAME TO {prior}')
            db.execute(ddl)
            db.execute(f'INSERT INTO {table} SELECT * FROM {prior}')
            for left, right in ((table, prior), (prior, table)):
                if db.execute(
                    f'SELECT * FROM {left} EXCEPT SELECT * FROM {right} LIMIT 1'
                ).fetchone() is not None:
                    raise ValueError('Journal rows changed; migration rolled back')
            db.execute(f'DROP TABLE {prior}')
            for (index,) in indices:
                db.execute(index)
            if db.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                raise ValueError('Journal integrity failure; migration rolled back')
            return {'rows': count, 'changed': True, 'all_rows_preserved': True}
    finally:
        template.unlink(missing_ok=True)


if __name__ == '__main__':
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('journal', type=Path)
    parser.add_argument('--template', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(migrate(args.journal, args.template)))
