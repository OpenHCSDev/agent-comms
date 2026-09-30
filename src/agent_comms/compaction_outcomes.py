"""Read-only projection of original selected outcomes, not another history."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .compaction_identity import SummaryOperationIdentity
from .compaction_records import CompactionOperation, SelectedSummaryAttempt
from .compaction_states import SummaryOutcome
from .field_codec import FieldCodec
from .text_digest import TextDigest
from .thread_identity import ThreadIncarnation
from .registry_document import RegistrySnapshot
from .selected_source import SessionRevision
from .coordination_errors import StaleRevision
from .transcript_events import NoticeTranscript
from .typed_table import TypedRow


@dataclass(frozen=True, kw_only=True)
class CompactionOutcomeTranscript(NoticeTranscript):
    """An original operation's notice, with unrecorded event time left unrecorded."""

    identity: SummaryOperationIdentity


@dataclass(frozen=True)
class JournalRowPosition(TypedRow):
    """SQLite's original row order, not an application sequence counter."""

    journal_rowid: int


@dataclass(frozen=True)
class SelectedCompactionOutcome:
    attempt: SelectedSummaryAttempt
    sequence: int

    @property
    def identity(self) -> SummaryOperationIdentity:
        return self.attempt.identity

    @property
    def native_offset(self) -> int:
        return self.source_revision[0][2]

    @property
    def source_revision(self) -> SessionRevision:
        """Keep original inode/cut/input-proof custody available to the source owner."""
        return self.attempt.source().reserved_revision

    def require_native_source(self, path: Path | str, through_offset: int) -> None:
        """Placement requires the original native inode and a complete captured cut."""
        from .backend import _session_revision

        if str(path) != self.identity.session_file:
            raise StaleRevision("Compaction outcome belongs to another native source")
        observed = _session_revision(str(path))
        if observed is None or observed[0][:2] != self.source_revision[0][:2]:
            raise StaleRevision("Compaction outcome native inode changed")
        if not self.native_offset <= through_offset <= observed[0][2]:
            raise StaleRevision("Compaction outcome native cut was truncated or not captured")

    @property
    def text(self) -> str:
        state: SummaryOutcome = self.attempt.state
        return state.outcome_text

    def event(self) -> CompactionOutcomeTranscript:
        return CompactionOutcomeTranscript(text=self.text, identity=self.identity)


@dataclass(frozen=True)
class CompactionOutcomeSnapshot:
    outcomes: tuple[SelectedCompactionOutcome, ...]

    @property
    def revision(self) -> TextDigest:
        """Late changes below the last row still change this scoped source epoch."""
        projection = [(row.sequence, row.source_revision, FieldCodec.encode(row.event()))
                      for row in self.outcomes]
        return TextDigest.of(json.dumps(projection, sort_keys=True, separators=(",", ":")))

    @classmethod
    def read(cls, db: sqlite3.Connection, session_file: str,
             incarnation: ThreadIncarnation, registry: RegistrySnapshot) -> CompactionOutcomeSnapshot:
        rows = SelectedSummaryAttempt.joined(db.execute(
            f"SELECT rowid AS journal_rowid,* FROM {SelectedSummaryAttempt.declared_name} "
            "WHERE session_file=? ORDER BY rowid", (session_file,)), JournalRowPosition)
        outcomes = []
        for attempt, position in rows:
            if attempt.source().incarnation.resolved(registry) != incarnation.resolved(registry):
                continue
            projected = attempt.state.project_outcome(attempt, position.journal_rowid)
            if projected is None:
                continue
            commits = CompactionOperation.select(
                db, where="session_file=? AND json_extract(intent_json, '$.selectedSummaryOperationId')=?",
                parameters=(session_file, attempt.operation_id),
            )
            if not any(commit.represents_summary(attempt) for commit in commits):
                outcomes.append(projected)
        return cls(tuple(outcomes))
