"""Process-local selected-summary input handoff capability.

A terminal-looking SQLite row is never input authority. Only a successfully
returned journal terminal transaction may mint this unpicklable one-use object.
The sole intended consumer is ACP's already wire-locked final send boundary:
it must durably bind an exact original input ID before stdin.write, and losing
the process before that write must not recreate a token from journal state.
No model tool, Pi RPC, or producer exposes this module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from .child_process import ProcessIdentity
from .compaction_errors import CompactionJournalError
from .compaction_identity import JournalCustody, ReturnedSummaryTerminal
from .compaction_journal import CompactionJournal
from .compaction_records import SelectedSummarySource
from .compaction_summaries import _consume_selected_ack
from .selected_source import SelectedAdmissionSource, SessionRevision, SessionRevisionUnavailable

if TYPE_CHECKING:
    from .compaction_states import CommittedNativeOutcome
    from .input_disposition import InputDispositions

_MINT = object()


@dataclass(frozen=True, slots=True)
class SelectedAdmissionIdentity:
    source: SelectedAdmissionSource
    session_revision: SessionRevision

    def matches_source(self, source: SelectedSummarySource) -> bool:
        return self.source == source.source


    def require_live_input(self, session: str, text: str) -> None:
        self.require_current_revision(session)
        if not self.source.input_digest.matches(text):
            raise CompactionJournalError("Selected input text changed")

    def require_current_revision(self, session: str) -> None:
        if self.source.owner != ProcessIdentity.capture(os.getpid()):
            raise CompactionJournalError("Selected input process incarnation changed")
        if not self.session_revision.current(session):
            raise CompactionJournalError("Selected native source revision changed")

    def require_current(self, source: SelectedSummarySource, session: str) -> None:
        if not self.matches_source(source):
            raise CompactionJournalError("Selected acknowledgment source changed")
        self.require_current_revision(session)

    def require_reserved_revision(self, session: str) -> None:
        self.require_current_revision(session)
        if self.session_revision != self.source.reserved_revision:
            raise CompactionJournalError("Selected decline source revision changed")

    def after_native_commit(self, session: str, evidence: CommittedNativeOutcome) -> SelectedAdmissionIdentity:
        try:
            revision = SessionRevision.observe(session).require_available()
        except SessionRevisionUnavailable as error:
            raise CompactionJournalError("Selected native result is unavailable: saved source missing") from error
        evidence.require_saved_revision(revision)
        if not revision.same_input_proof(self.source.reserved_revision):
            raise CompactionJournalError("Selected native result is unavailable: input proof changed")
        return replace(self, session_revision=revision)


class SelectedSummaryAdmission:
    """One-use, unpicklable owner-process capability; no replay after fault."""

    __slots__ = ("_terminal", "_identity", "_custody", "_used")

    def __init__(
        self, key: object, terminal: ReturnedSummaryTerminal, identity: SelectedAdmissionIdentity,
    ) -> None:
        if key is not _MINT:
            raise TypeError("Selected admission cannot be constructed by a caller")
        self._terminal = terminal
        self._identity = identity
        self._custody = JournalCustody.capture(terminal.path)
        self._used = False

    def __reduce__(self) -> NoReturn:
        raise TypeError("Selected admission cannot cross a process boundary")

    @property
    def original_source(self) -> SelectedAdmissionSource:
        """The original immutable reservation; observing it grants no new token."""
        return self._identity.source

    @classmethod
    def _from_returned_ack(
        cls, receipt: object, terminal: ReturnedSummaryTerminal, identity: SelectedAdmissionIdentity,
    ) -> SelectedSummaryAdmission:
        if not _consume_selected_ack(receipt, terminal):
            raise CompactionJournalError("Exact returned terminal fsync ACK required")
        try:
            attempt = terminal.attempt
            source = attempt.request
            identity.require_current(source, attempt.session_file)
            attempt.state.require_original_admission()
        except (TypeError, ValueError, OSError) as error:
            raise CompactionJournalError("Selected acknowledgment identity or source changed") from error
        return cls(_MINT, terminal, identity)

    def invalidate(self) -> None:
        """Burn a stale owner/turn attempt before a future authority ABA."""
        self._used = True

    def consume_bound_original(
        self,
        *,
        wire_root: Path,
        session_file: str,
        identity: SelectedAdmissionIdentity,
        native_id: str,
        sent_text: str,
        dispositions: InputDispositions,
    ) -> bool:
        """Consume before durable bind; call ONLY inside ACP's final wire lock.

        ACP verifies its current owner/turn/ingress/correction snapshot first,
        and keeps the same lock through stdin.write. A failed bind or exception
        consumes the token and cannot be retried, even within this process.
        """
        if self._used:
            return False
        self._used = True
        # The token is consumed. A changed selected authority is this
        # admission's typed refusal; storage and decoding failures propagate
        # as uncertain operations, never as a retryable policy refusal.
        try:
            if JournalCustody.capture(wire_root / "compaction-commits.sqlite3") != self._custody:
                return False
            if identity != self._identity:
                return False
            attempt = self._terminal.attempt
            if str(Path(session_file).resolve(strict=True)) != attempt.session_file:
                return False
            identity.require_live_input(session_file, sent_text)
            journal = CompactionJournal(self._terminal.path)
            journal.summaries.require_original_admission(attempt)
            attempt.request.reservation_check(
                identity.source.reserved_revision, dispositions.read()
            ).require_valid()
            return dispositions.bind_originals(
                identity.source.ingress_keys,
                admission=identity.source.admission_generation,
                turn_id=identity.source.turn.value,
                native_id=native_id,
                text=sent_text,
            )
        except CompactionJournalError:
            return False
