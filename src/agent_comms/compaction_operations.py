"""Native commit reservation and atomic outcome/publication persistence."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from uuid import uuid4

from .compaction_errors import CompactionJournalError
from .compaction_identity import SelectedCommitReference
from .compaction_journal_role import JournalRole
from .compaction_records import CompactionOperation, CompactionPublication, SelectedSummaryAttempt
from .compaction_states import (
    IntentOperation,
    NativeOutcome,
    PendingPublication,
)
from .compaction_source import CompactionSource
from .input_disposition import InputDocument
from .native_compaction_request import NativeIntent
from .owner_compaction_gate import OwnerCompactionAttestation


class NativeOperations(JournalRole):
    def begin(
        self,
        session_file: str,
        intent: NativeIntent,
        *,
        inputs: InputDocument,
        owner: OwnerCompactionAttestation,
        source: CompactionSource,
        selected: SelectedCommitReference | None = None,
        commit_id: str | None = None,
    ) -> str:
        """Durably reserve under the caller's retained input snapshot lock.

        OwnerCompactionCommit already holds the exclusive disposition lock;
        consuming that snapshot avoids reacquisition and preserves input-before-
        journal ordering. Every reused ID is forbidden forever.
        """
        commit_id = uuid4().hex if commit_id is None else commit_id
        if not re.fullmatch(r"[0-9a-f]{32}", commit_id):
            raise ValueError("Expected exact compaction commit ID")
        canonical = str(Path(session_file).resolve(strict=True))
        intent.witness.require_session(canonical)
        payload = intent.journal_json(owner, source, selected)
        try:
            with self.journal.transaction() as db:
                attempts = SelectedSummaryAttempt.blocking_in(db, canonical, inputs)
                if attempts:
                    if len(attempts) != 1:
                        raise CompactionJournalError(
                            "Blocked selected summary; unrelated native commit forbidden"
                        )
                    if selected is None:
                        raise CompactionJournalError(
                            "Blocked selected summary; unrelated native commit forbidden"
                        )
                    if selected.identity(canonical) != attempts[0].identity:
                        raise CompactionJournalError(
                            "Blocked selected summary; unrelated native commit forbidden"
                        )
                    attempts[0].state.require_commit_reservation()
                CompactionOperation(commit_id, canonical, payload, IntentOperation(), None).insert(
                    db
                )
        except sqlite3.IntegrityError as error:
            raise CompactionJournalError(
                "Unresolved session or reused commit ID; never replay"
            ) from error
        return commit_id

    def get(self, commit_id: str) -> CompactionOperation:
        with self.journal.transaction() as db:
            row = CompactionOperation.one(db, commit_id=commit_id)
        if row is None:
            raise CompactionJournalError("Unknown compaction operation")
        return row

    def unresolved(self, session_file: str) -> tuple[CompactionOperation, ...]:
        """Discover crash-orphaned intents for explicit recovery, never dispatch."""
        canonical = str(Path(session_file).resolve(strict=True))
        with self.journal.transaction() as db:
            rows = CompactionOperation.unresolved_in(db, canonical)
        return tuple(rows)

    def resolve(
        self, commit_id: str, outcome: NativeOutcome
    ) -> None:
        """Persist bridge-validated native evidence; this does not verify it.

        Unknown/intent remains blocking. A terminal outcome is immutable.
        The observed NativeOutcome owns both its operation disposition and
        publication metadata. Once UNKNOWN, only writer-fenced exact-ID
        reconciliation may establish committed or aborted-no-write.
        """
        metadata = outcome.publication_json(commit_id)
        payload = outcome.journal_json()
        with self.journal.transaction() as db:
            row = CompactionOperation.one(db, commit_id=commit_id)
            if row is None or not row.state.may_become(outcome.state):
                raise CompactionJournalError("Compaction outcome transition forbidden")
            CompactionOperation.update(
                db,
                where="commit_id=?",
                parameters=(commit_id,),
                state=outcome.state,
                evidence_json=payload,
            )
            if metadata is not None:
                CompactionPublication(
                    commit_id, row.session_file, metadata, PendingPublication()
                ).insert(db)
