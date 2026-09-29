"""Native commit reservation and atomic outcome/publication persistence."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from uuid import uuid4

from .compaction_errors import CompactionJournalError
from .compaction_identity import (
    SelectedCommitReference,
)
from .compaction_journal_role import JournalRole
from .compaction_records import CompactionOperation, CompactionPublication, SelectedSummaryAttempt
from .compaction_states import (
    CommittedNativeOutcome,
    IntentOperation,
    OperationState,
    PendingPublication,
)
from .field_codec import FieldCodec
from .input_disposition import InputDocument


class NativeOperations(JournalRole):
    def begin(
        self,
        session_file: str,
        intent: dict,
        *,
        inputs: InputDocument,
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
        payload = json.dumps(intent, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(payload.encode()) > 65536:
            raise ValueError("Compaction intent exceeds bound")
        try:
            with self.journal.transaction() as db:
                selected = SelectedSummaryAttempt.blocking_in(db, canonical, inputs)
                if selected:
                    if len(selected) != 1:
                        raise CompactionJournalError(
                            "Blocked selected summary; unrelated native commit forbidden"
                        )
                    try:
                        reference = SelectedCommitReference.from_intent(intent)
                    except (TypeError, ValueError) as error:
                        raise CompactionJournalError(
                            "Blocked selected summary; unrelated native commit forbidden"
                        ) from error
                    if reference.identity(canonical) != selected[0].identity:
                        raise CompactionJournalError(
                            "Blocked selected summary; unrelated native commit forbidden"
                        )
                    selected[0].state.require_commit_reservation()
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
        self, commit_id: str, outcome: OperationState, evidence: dict, *, publication: bool = False
    ) -> None:
        """Persist bridge-validated native evidence; this does not verify it.

        Unknown/intent remains blocking. A terminal outcome is immutable.
        ``refused`` is only allowed directly after intent, for a proven
        pre-write native refusal. Once UNKNOWN, only writer-fenced exact-ID
        reconciliation may establish committed or aborted-no-write.
        """
        metadata = None
        if publication:
            outcome.require_committed(commit_id)
            try:
                metadata = FieldCodec.decode(CommittedNativeOutcome, evidence).publication_json(
                    commit_id
                )
            except (ValueError, TypeError) as error:
                raise CompactionJournalError("Exact committed native metadata required") from error
        payload = json.dumps(evidence, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(payload.encode()) > 65536:
            raise ValueError("Compaction outcome exceeds bound")
        with self.journal.transaction() as db:
            row = CompactionOperation.one(db, commit_id=commit_id)
            if row is None or not row.state.may_become(outcome):
                raise CompactionJournalError("Compaction outcome transition forbidden")
            CompactionOperation.update(
                db,
                where="commit_id=?",
                parameters=(commit_id,),
                state=outcome,
                evidence_json=payload,
            )
            if metadata is not None:
                CompactionPublication(
                    commit_id, row.session_file, metadata, PendingPublication()
                ).insert(db)
