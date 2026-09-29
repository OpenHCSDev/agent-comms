"""Exact native publication observation over the canonical journal."""

from __future__ import annotations

from pathlib import Path

from .compaction_errors import CompactionJournalError
from .compaction_journal_role import JournalRole
from .compaction_records import CompactionOperation, CompactionPublication
from .compaction_states import (
    ObservedPublication,
    PendingPublication,
)


class CompactionPublications(JournalRole):
    def pending(self, session_file: str) -> tuple[CompactionPublication, ...]:
        """Read exact-ID metadata; an unknown commit cannot be projected."""
        canonical = str(Path(session_file).resolve(strict=True))
        with self.journal.transaction() as db:
            rows = CompactionPublication.select(
                db,
                where="session_file=? AND json_extract(state, '$.kind')=? ORDER BY rowid LIMIT 32",
                parameters=(canonical, PendingPublication.declared_name),
            )
            for row in rows:
                operation = CompactionOperation.one(db, commit_id=row.commit_id)
                if operation is None:
                    raise CompactionJournalError("Compaction publication metadata changed")
                row.require_operation(operation)
        return tuple(rows)

    def observe(self, commit_id: str, metadata_json: str) -> None:
        """ACK only the exact metadata seen by the local ACP projection.

        Delivery must return before this call. A failure before marking leaves
        the row pending. A post-COMMIT fsync error may leave it *observed* even
        though this call raises UNKNOWN; reconcile the exact row, never infer
        native retry authority. Reprojection may repeat a pending commit ID, so
        consumers must deduplicate by ID, never by summary text.
        """
        with self.journal.transaction() as db:
            row = CompactionPublication.one(db, commit_id=commit_id)
            if row is None:
                raise CompactionJournalError("Unknown or changed publication metadata")
            row.require_metadata(metadata_json)
            if row.state.may_become(ObservedPublication()):
                CompactionPublication.update(
                    db, where="commit_id=?", parameters=(commit_id,), state=ObservedPublication()
                )
