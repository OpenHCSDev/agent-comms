"""Exact-session native compaction barrier before ANY ACP provider input send.

This is a read-only policy gate. Unknown/intents require explicit exact-ID native
reconciliation; neither an ACP correction nor a fresh session may replay them.
"""

from __future__ import annotations

from pathlib import Path

from .compaction_journal import CompactionJournal, CompactionJournalError


def native_input_admitted(wire_root: Path, session_file: str | None) -> bool:
    """Fail closed if this saved session has an unresolved native commit.

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
        return not CompactionJournal(path).unresolved(session_file)
    except (OSError, ValueError, CompactionJournalError):
        return False
