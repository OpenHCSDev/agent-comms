"""Process-local selected-summary input handoff capability.

A terminal-looking SQLite row is never input authority. Only a successfully
returned journal terminal transaction may mint this unpicklable one-use object.
The sole intended consumer is ACP's already wire-locked final send boundary:
it must durably bind an exact original input ID before stdin.write, and losing
the process before that write must not recreate a token from journal state.
No model tool, Pi RPC, or producer exposes this module.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from .backend import _session_revision
from .child_process import ProcessIdentity
from .compaction_journal import (
    CompactionJournal,
    CompactionJournalError,
    SelectedSummarySource,
    _consume_selected_ack,
)
from .compaction_states import SummaryState
from .field_codec import FieldCodec
from .reservation_rules import ReservationViolationError
from .selected_source import SelectedAdmissionSource, SessionRevision

if TYPE_CHECKING:
    from .input_disposition import InputDispositions

_MINT = object()


@dataclass(frozen=True, slots=True)
class SelectedAdmissionIdentity:
    source: SelectedAdmissionSource
    session_revision: SessionRevision

    def matches_source(self, source: SelectedSummarySource) -> bool:
        return self.source == source.source


class SelectedSummaryAdmission:
    """One-use, unpicklable owner-process capability; no replay after fault."""

    __slots__ = (
        "_path",
        "_session",
        "_operation_id",
        "_state",
        "_source_json",
        "_identity",
        "_process_pid",
        "_journal_inode",
        "_used",
    )

    def __init__(
        self,
        key: object,
        path: Path,
        session: str,
        operation_id: str,
        state: SummaryState,
        source_json: str,
        identity: SelectedAdmissionIdentity,
    ) -> None:
        if key is not _MINT:
            raise TypeError("Selected admission cannot be constructed by a caller")
        self._path = path
        self._session = session
        self._operation_id = operation_id
        self._state = state
        self._source_json = source_json
        self._identity = identity
        self._process_pid = os.getpid()
        stat = path.stat()
        self._journal_inode = (stat.st_dev, stat.st_ino)
        self._used = False

    def __reduce__(self) -> NoReturn:
        raise TypeError("Selected admission cannot cross a process boundary")

    @classmethod
    def _from_returned_ack(
        cls,
        receipt: object,
        path: Path,
        session: str,
        operation_id: str,
        state: SummaryState,
        source_json: str,
        identity: SelectedAdmissionIdentity,
    ) -> SelectedSummaryAdmission:
        scope = (str(path), session, operation_id, state, source_json)
        if not _consume_selected_ack(receipt, scope):
            raise CompactionJournalError("Exact returned terminal fsync ACK required")
        try:
            source = FieldCodec.decode(SelectedSummarySource, json.loads(source_json))
            valid = (
                identity.matches_source(source)
                and identity.source.owner == ProcessIdentity.capture(os.getpid())
                and identity.session_revision is not None
                and _session_revision(session) == identity.session_revision
                and state.original_eligible
            )
        except (TypeError, ValueError, OSError):
            valid = False
        if not valid:
            raise CompactionJournalError("Selected acknowledgment identity or source changed")
        return cls(_MINT, path, session, operation_id, state, source_json, identity)

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
        try:
            if (
                os.getpid() != self._process_pid
                or identity != self._identity
                or identity.source.owner != ProcessIdentity.capture(os.getpid())
                or Path(session_file).resolve(strict=True) != Path(self._session)
                or wire_root / "compaction-commits.sqlite3" != self._path
                or (
                    (stat := self._path.stat()).st_dev,
                    stat.st_ino,
                )
                != self._journal_inode
                or _session_revision(session_file) != identity.session_revision
                or not identity.source.input_digest.matches(sent_text)
            ):
                return False
            journal = CompactionJournal(self._path)
            attempt = journal.selected_summary(self._operation_id)
            if (
                attempt.session_file != self._session
                or attempt.state != self._state
                or attempt.source_json != self._source_json
                or len(journal.blocking_selected_summary(session_file)) != 1
                or journal.unresolved(session_file)
            ):
                return False
            if not self._state.verifies_original(
                journal, self._session, self._operation_id, self._source_json
            ):
                return False
            identity.source.reservation_check(
                identity.source.reserved_revision, dispositions.read()
            ).require_valid()
            return dispositions.bind(
                identity.source.ingress_key,
                admission=identity.source.admission_generation,
                turn_id=identity.source.turn.value,
                native_id=native_id,
                text=sent_text,
            )
        except ReservationViolationError:
            raise
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, CompactionJournalError):
            return False
