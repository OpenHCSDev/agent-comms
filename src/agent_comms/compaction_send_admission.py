"""Exact-session compaction barrier before ANY ACP provider input send.

Unresolved native commits or selected-summary reservations/UNKNOWN attempts
refuse input. Neither an ACP correction nor a fresh session may replay them.
"""

from __future__ import annotations

from pathlib import Path

from .compaction_journal import CompactionJournal, CompactionJournalError


def native_input_admitted(wire_root: Path, session_file: str | None) -> bool:
    """Fail closed on an unresolved native commit or selected summary attempt.

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
        return not journal.unresolved(session_file) and not journal.unresolved_selected_summary(
            session_file
        )
    except (OSError, ValueError, CompactionJournalError):
        return False
