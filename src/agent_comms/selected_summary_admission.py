"""Default-OFF, process-local selected-summary input handoff capability.

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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from .backend import _session_revision
from .compaction_journal import CompactionJournal, CompactionJournalError, _consume_selected_ack

if TYPE_CHECKING:
    from .input_disposition import InputDispositions

_HEX = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_FIELDS = {
    "ownerName",
    "ownerPid",
    "ownerCreatedAt",
    "turnId",
    "ingressKey",
    "admissionGeneration",
    "correctionWitness",
    "inputSha256",
    "originalSha256",
    "reservedRevision",
}
_MINT = object()


@dataclass(frozen=True, slots=True)
class SelectedAdmissionIdentity:
    owner_name: str
    owner_pid: int
    owner_created_at: str
    turn_id: str
    ingress_key: str
    admission_generation: int
    correction_witness: str
    input_sha256: str
    original_sha256: str
    # The durable reservation witnesses the pre-result saved session bytes.
    reserved_revision: tuple[tuple[int, int, int, int, int], tuple[int, int, int, int, int] | None]
    # This is the *post-result* native session revision, or the unchanged
    # revision on a clean pre-start decline. Recheck it at final send.
    session_revision: tuple[tuple[int, int, int, int, int], tuple[int, int, int, int, int] | None]

    def matches_source(self, source: dict) -> bool:
        witness = source.get("source")
        return (
            type(witness) is dict
            and set(witness) == _SOURCE_FIELDS
            and witness == self.source_fields()
            and type(self.owner_name) is str
            and bool(self.owner_name)
            and type(self.owner_pid) is int
            and self.owner_pid > 0
            and type(self.owner_created_at) is str
            and bool(self.owner_created_at)
            and type(self.turn_id) is str
            and bool(self.turn_id)
            and type(self.ingress_key) is str
            and bool(self.ingress_key)
            and type(self.admission_generation) is int
            and self.admission_generation > 0
            and type(self.correction_witness) is str
            and bool(self.correction_witness)
            and type(self.input_sha256) is str
            and _HEX.fullmatch(self.input_sha256) is not None
            and type(self.original_sha256) is str
            and _HEX.fullmatch(self.original_sha256) is not None
            and self.reserved_revision is not None
        )

    def source_fields(self) -> dict:
        """The admission identity owns its existing persisted wire format."""
        return {
            "ownerName": self.owner_name,
            "ownerPid": self.owner_pid,
            "ownerCreatedAt": self.owner_created_at,
            "turnId": self.turn_id,
            "ingressKey": self.ingress_key,
            "admissionGeneration": self.admission_generation,
            "correctionWitness": self.correction_witness,
            "inputSha256": self.input_sha256,
            "originalSha256": self.original_sha256,
            "reservedRevision": json.loads(json.dumps(self.reserved_revision)),
        }


class SelectedSummaryAdmission:
    """One-use, unpicklable owner-process capability; no replay after fault."""

    __slots__ = (
        "_path",
        "_session",
        "_operation_id",
        "_status",
        "_commit_id",
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
        status: str,
        commit_id: str | None,
        source_json: str,
        identity: SelectedAdmissionIdentity,
    ) -> None:
        if key is not _MINT:
            raise TypeError("Selected admission cannot be constructed by a caller")
        self._path = path
        self._session = session
        self._operation_id = operation_id
        self._status = status
        self._commit_id = commit_id
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
        status: str,
        commit_id: str | None,
        source_json: str,
        identity: SelectedAdmissionIdentity,
    ) -> SelectedSummaryAdmission:
        scope = (str(path), session, operation_id, status, commit_id, source_json)
        if not _consume_selected_ack(receipt, scope):
            raise CompactionJournalError("Exact returned terminal fsync ACK required")
        try:
            source = json.loads(source_json)
            valid = (
                type(source) is dict
                and identity.matches_source(source)
                and identity.owner_pid == os.getpid()
                and identity.session_revision is not None
                and _session_revision(session) == identity.session_revision
                and (status == "linked" or status == "declined-prestart")
            )
        except (TypeError, ValueError, OSError):
            valid = False
        if not valid:
            raise CompactionJournalError("Selected acknowledgment identity or source changed")
        return cls(_MINT, path, session, operation_id, status, commit_id, source_json, identity)

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
                or type(identity) is not SelectedAdmissionIdentity
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
                or attempt.status != self._status
                or attempt.commit_id != self._commit_id
                or attempt.source_json != self._source_json
                or len(journal.blocking_selected_summary(session_file)) != 1
                or journal.unresolved(session_file)
            ):
                return False
            if self._status == "linked":
                if self._commit_id is None:
                    return False
                commit = journal.get(self._commit_id)
                intent = json.loads(commit.intent_json)
                if (
                    commit.session_file != self._session
                    or commit.status != "committed"
                    or type(intent) is not dict
                    or intent.get("selectedSummaryOperationId") != self._operation_id
                    or intent.get("selectedSummarySourceDigest")
                    != hashlib.sha256(self._source_json.encode()).hexdigest()
                ):
                    return False
            row = dispositions.get(identity.ingress_key)
            if (
                row is None
                or row["status"] != "unknown"
                or row["owner"] != identity.owner_name
                or row["admission"] != identity.admission_generation
                or row["native_id"] is not None
                or type(row["source_text"]) is not str
                or hashlib.sha256(row["source_text"].encode()).hexdigest()
                != identity.original_sha256
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
