"""Read-only backup of a real old journal; candidate must refuse without conversion."""

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from agent_comms.compaction_journal import CompactionJournal, CompactionJournalError


def main():
    source, candidate = (Path(value).absolute() for value in sys.argv[1:])
    assert not candidate.exists()
    with (
        closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as old,
        closing(sqlite3.connect(candidate)) as copy,
    ):
        old.backup(copy)

    def snapshot():
        with closing(sqlite3.connect(candidate.as_uri() + "?mode=ro", uri=True)) as db:
            names = [
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            ]
            assert all(name.replace("_", "").isalnum() for name in names)
            return {name: db.execute(f'SELECT * FROM "{name}"').fetchall() for name in names}

    before = snapshot()
    try:
        CompactionJournal(candidate)
    except CompactionJournalError as error:
        assert "quiet cutover" in str(error)
    else:
        raise AssertionError("Old journal was accepted without quiet reset")
    assert snapshot() == before
    print(
        json.dumps(
            {
                "source": str(source),
                "rows": {name: len(rows) for name, rows in before.items()},
                "candidate_refused_old_schema": True,
                "copy_rows_unchanged": True,
                "source_open_mode": "read-only",
                "no_live_mutations": True,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
