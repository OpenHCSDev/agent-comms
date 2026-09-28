"""Process-local selected-summary input handoff capability.

A terminal-looking SQLite row is never input authority. Only a successfully
returned journal terminal transaction may mint this unpicklable one-use object.
The sole intended consumer is ACP's already wire-locked final send boundary:
it must durably bind an exact original input ID before stdin.write, and losing
the process before that write must not recreate a token from journal state.
No model tool, Pi RPC, or producer exposes this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from .backend import _session_revision
from .compaction_journal import (
    CompactionJournal,
    CompactionJournalError,
    SelectedSummarySource,
    _consume_selected_ack,
)
from .compaction_states import SummaryState
from .field_codec import FieldCodec

if TYPE_CHECKING:
    from .input_disposition import InputDispositions

_HEX = re.compile(r"[0-9a-f]{64}\Z")
_MINT = object()


@dataclass(frozen=True, slots=True)
class SelectedAdmissionSource:
    owner_name: str = field(metadata={"wire_name": "ownerName"})
    owner_pid: int = field(metadata={"wire_name": "ownerPid"})
    owner_created_at: str = field(metadata={"wire_name": "ownerCreatedAt"})
    turn_id: str = field(metadata={"wire_name": "turnId"})
    ingress_key: str = field(metadata={"wire_name": "ingressKey"})
    admission_generation: int = field(metadata={"wire_name": "admissionGeneration"})
    correction_witness: str = field(metadata={"wire_name": "correctionWitness"})
    input_sha256: str = field(metadata={"wire_name": "inputSha256"})
    original_sha256: str = field(metadata={"wire_name": "originalSha256"})
    reserved_revision: tuple[
        tuple[int, int, int, int, int], tuple[int, int, int, int, int] | None
    ] = field(metadata={"wire_name": "reservedRevision"})

    def __post_init__(self):
        if (
            not all(
                (
                    self.owner_name,
                    self.owner_created_at,
                    self.turn_id,
                    self.ingress_key,
                    self.correction_witness,
                )
            )
            or self.owner_pid <= 0
            or self.admission_generation <= 0
            or _HEX.fullmatch(self.input_sha256) is None
            or _HEX.fullmatch(self.original_sha256) is None
        ):
            raise ValueError("Exact selected admission identity required")


@dataclass(frozen=True, slots=True)
class SelectedAdmissionIdentity(SelectedAdmissionSource):
    # Post-result revision; reserved_revision retains the pre-result witness.
    session_revision: tuple[
        tuple[int, int, int, int, int], tuple[int, int, int, int, int] | None
    ] = field(metadata={"source_exclude": True})

    def matches_source(self, source: SelectedSummarySource) -> bool:
        try:
            witness = FieldCodec.decode(SelectedAdmissionSource, source.source)
        except (TypeError, ValueError):
            return False
        return FieldCodec.encode(witness) == FieldCodec.project(self, "source")


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
                and identity.owner_pid == os.getpid()
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
                or identity.owner_pid != os.getpid()
                or Path(session_file).resolve(strict=True) != Path(self._session)
                or wire_root / "compaction-commits.sqlite3" != self._path
                or (
                    (stat := self._path.stat()).st_dev,
                    stat.st_ino,
                )
                != self._journal_inode
                or _session_revision(session_file) != identity.session_revision
                or hashlib.sha256(sent_text.encode()).hexdigest() != identity.input_sha256
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
            row = dispositions.read().rows.get(identity.ingress_key)
            if (
                row is None
                or not row.unresolved
                or row.owner != identity.owner_name
                or row.admission != identity.admission_generation
                or row.native_id is not None
                or hashlib.sha256(row.source_text.encode()).hexdigest() != identity.original_sha256
            ):
                return False
            return dispositions.bind(
                identity.ingress_key,
                admission=identity.admission_generation,
                turn_id=identity.turn_id,
                native_id=native_id,
                text=sent_text,
            )
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, CompactionJournalError):
            return False
