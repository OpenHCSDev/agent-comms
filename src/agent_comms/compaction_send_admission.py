"""Exact-session compaction barrier before ANY ACP provider input send.

Unresolved native commits or incomplete selected-summary attempts refuse input. A
terminal-looking selected row alone is not admission. Completion is derived
from the original input ledger's native-start evidence; it never permits replay.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .compaction_errors import CompactionJournalError
from .compaction_journal import CompactionJournal


def native_input_admitted(wire_root: Path, session_file: str | None) -> bool:
    """Fail closed on an unresolved commit or an incomplete selected attempt.

    No journal is normal before the owner has ever prepared a native commit.
    Call under the wire lock immediately before the backend's stdin write; the
    commit owner holds this lock throughout native mutation and outcome sync.
    """
    if not session_file:
        return True
    path = wire_root / "compaction-commits.sqlite3"
    try:
        if not path.exists() and not path.is_symlink():
            return True
        journal = CompactionJournal(path)
        return not journal.operations.unresolved(session_file) and not journal.summaries.blocking(
            session_file
        )
    except (OSError, ValueError, sqlite3.Error, CompactionJournalError):
        return False
