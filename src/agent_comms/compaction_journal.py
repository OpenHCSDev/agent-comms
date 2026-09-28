"""Durable, non-replayable compaction intents (not an authority mechanism).

The owner commit bridge must call this inside its registry authority scope.
Native reconciliation evidence must be collected under the native writer lock
before calling ``resolve``. No method here dispatches or retries native work.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4
from weakref import WeakKeyDictionary

from .input_attempt import InputAttempt
from .input_disposition import InputDispositions, InputDocument

if TYPE_CHECKING:
    from .fresh_private_session import FreshPrivateSession
    from .selected_summary_admission import SelectedAdmissionIdentity, SelectedSummaryAdmission

from .compaction_states import (
    CommittedOperation,
    DeclinedPrestartSummary,
    IntentOperation,
    LinkedSummary,
    ObservedPublication,
    OperationState,
    PendingPublication,
    PublicationState,
    RefusedSummary,
    ReservedSummary,
    SummaryState,
    UnknownSummary,
    sql_names,
)


class _ReturnedTerminalAck:
    """Only the successful post-COMMIT parent fsync creates this identity."""

    __slots__ = ("__weakref__",)


_issued_selected_acks: WeakKeyDictionary[_ReturnedTerminalAck, tuple] = WeakKeyDictionary()
# A visible enrollment SQL row after lost parent fsync is NOT coverage authority.
# Only its exact returned registration can allow a same-process private attempt.
_returned_fresh_enrollments: WeakKeyDictionary[FreshPrivateSession, tuple] = WeakKeyDictionary()


def _consume_selected_ack(receipt: object, expected: tuple) -> bool:
    """Consume exact one-use post-fsync receipt, never derive one from a row."""
    if type(receipt) is not _ReturnedTerminalAck:
        return False
    recorded = _issued_selected_acks.pop(receipt, None)
    return recorded == expected


class CompactionJournalError(RuntimeError):
    """Journal state cannot authorize another dispatch; fail closed."""


class CompactionJournalUnknownError(CompactionJournalError):
    """A COMMIT may be visible but lacks durable confirmation; never dispatch/replay."""


@dataclass(frozen=True)
class CompactionOperation:
    commit_id: str
    session_file: str
    intent_json: str
    state: OperationState
    evidence_json: str | None

    @classmethod
    def from_row(cls, row):
        commit_id, session, intent, status, evidence = row
        return cls(commit_id, session, intent, OperationState.decode(status)(), evidence)


@dataclass(frozen=True)
class SelectedSummaryAttempt:
    """Provider attempt reservation, not a summary or native commit receipt."""

    operation_id: str
    session_file: str
    source_json: str
    state: SummaryState

    @classmethod
    def from_row(cls, row):
        operation_id, session, source, status, commit_id, reason = row
        try:
            state = SummaryState.from_columns(status, commit_id, reason)
        except (TypeError, ValueError) as error:
            raise CompactionJournalError(
                "Invalid saved selected summary state; never replay"
            ) from error
        return cls(operation_id, session, source, state)

    def original_has_started(self, inputs: dict[str, InputAttempt]) -> bool:
        """Completed input evidence retires this barrier, never recreates a send token.

        The existing input ledger owns native-start proof. A linked/declined
        summary alone, a bound UNKNOWN input, or an unrelated started input
        cannot retire the reservation. Historical rows and IDs stay intact.
        """
        if not self.state.original_eligible:
            return False
        try:
            source = json.loads(self.source_json)["source"]
            key = source["ingressKey"]
            if not isinstance(key, str) or not key.startswith("acp:"):
                return False
            row = inputs.get(key)
            return row is not None and (
                not row.unresolved
                and row.native_id is not None
                and row.sequence is None
                and row.owner == row.target == source["ownerName"]
                and row.admission == source["admissionGeneration"]
                and row.turn_id == source["turnId"]
                and row.sent_text is not None
                and hashlib.sha256(row.sent_text.encode()).hexdigest() == source["inputSha256"]
                and hashlib.sha256(row.source_text.encode()).hexdigest() == source["originalSha256"]
            )
        except (KeyError, TypeError, ValueError):
            return False


@dataclass(frozen=True)
class CompactionPublication:
    """Local metadata-only projection, keyed by native commit ID; no recipient."""

    commit_id: str
    session_file: str
    metadata_json: str
    state: PublicationState


def _publication_metadata(commit_id: str, evidence: dict) -> str:
    if (
        set(evidence)
        not in (
            {"status", "entryId", "revision", "leafId"},
            {"status", "entryId", "revision", "leafId", "metadataDigest"},
        )
        or evidence.get("status") != CommittedOperation.declared_name
        or (
            "metadataDigest" in evidence
            and (
                type(evidence["metadataDigest"]) is not str
                or len(evidence["metadataDigest"]) != 64
                or any(c not in "0123456789abcdef" for c in evidence["metadataDigest"])
            )
        )
        or any(
            type(evidence[key]) is not str or not evidence[key]
            for key in ("entryId", "revision", "leafId")
        )
    ):
        raise CompactionJournalError("Exact committed native metadata required")
    return json.dumps(
        {
            "commitId": commit_id,
            "entryId": evidence["entryId"],
            "revision": evidence["revision"],
            "leafId": evidence["leafId"],
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


class CompactionJournal:
    """One durable journal per wire; at most one unresolved op per session.

    Parent directory must already exist (the registry owns its creation).
    Verified SQLite EXTRA plus post-COMMIT directory fsync make successful
    ``begin`` durable before dispatch. If begin raises, DO NOT dispatch; if
    outcome persistence raises, the durable intent remains unresolved.
    """

    def __init__(self, path: Path):
        self.path = path
        if os.name != "posix":
            raise NotImplementedError("Compaction journal bridge requires POSIX")
        # Claim a private file before SQLite opens it; never truncate existing
        # history. Registry serialization covers normal callers; SQLite also
        # protects uniqueness against independent connections.
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if path.is_symlink() or not path.is_file():
                raise CompactionJournalError("Journal must be a regular private file") from None
        else:
            os.close(fd)
        with self._transaction() as db:
            db.execute(f"""CREATE TABLE IF NOT EXISTS operations (
                    commit_id TEXT PRIMARY KEY,
                    session_file TEXT NOT NULL,
                    intent_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN
                        {sql_names(OperationState)}),
                    evidence_json TEXT
                )""")
            db.execute(f"""CREATE UNIQUE INDEX IF NOT EXISTS unresolved_session
                ON operations(session_file) WHERE status IN {sql_names(OperationState, unresolved=True)}""")
            db.execute(f"""CREATE TABLE IF NOT EXISTS publications (
                    commit_id TEXT PRIMARY KEY REFERENCES operations(commit_id),
                    session_file TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN {sql_names(PublicationState)})
                )""")
            db.execute(f"""CREATE TABLE IF NOT EXISTS selected_summary_attempts (
                    operation_id TEXT PRIMARY KEY,
                    session_file TEXT NOT NULL,
                    source_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN
                        {sql_names(SummaryState)}),
                    commit_id TEXT,
                    decline_reason TEXT
                )""")
            # 47c8's partial index permitted several historical terminal rows.
            # Preserve every row: a global UNIQUE migration would fail journal
            # open and block unrelated sessions. BEGIN IMMEDIATE + the SELECT
            # before reserve below excludes incomplete attempts for that session.
            db.execute("DROP INDEX IF EXISTS selected_summary_session")
            db.execute(f"""CREATE UNIQUE INDEX IF NOT EXISTS unresolved_selected_summary_session
                ON selected_summary_attempts(session_file)
                WHERE status IN {sql_names(SummaryState, unresolved=True)}""")
            # Exact saved-session prewrite barrier for PR94's private raw writer.
            # An UNKNOWN marker is never cleared by a pipe ACK or fake result.
            db.execute("""CREATE TABLE IF NOT EXISTS private_raw_inputs (
                    input_id TEXT PRIMARY KEY,
                    session_file TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status = 'unknown')
                )""")
            db.execute("""CREATE TABLE IF NOT EXISTS enrolled_private_sessions (
                    session_file TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL UNIQUE,
                    device INTEGER NOT NULL,
                    inode INTEGER NOT NULL,
                    header_sha256 TEXT NOT NULL,
                    owner_name TEXT NOT NULL,
                    owner_created_at TEXT NOT NULL,
                    owner_lookup TEXT NOT NULL,
                    owner_generation INTEGER NOT NULL,
                    admission_epoch INTEGER NOT NULL,
                    creator_pid INTEGER NOT NULL
                )""")
        parent = path.parent.resolve(strict=True)
        for directory in (parent, *parent.parents):
            parent_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Durable generic transaction; never mints a selected admission ACK.

        Only the verified terminal methods below issue their one-use receipt,
        after this exact transaction's commit and parent fsync have returned.
        A caller's status-only SQL transition cannot use this generic API to
        construct input authority.
        """
        db = sqlite3.connect(self.path, timeout=5)
        try:
            mode = db.execute("PRAGMA journal_mode=DELETE").fetchone()
            db.execute("PRAGMA synchronous=EXTRA")
            if mode != ("delete",) or db.execute("PRAGMA synchronous").fetchone() != (3,):
                raise CompactionJournalError("Required durable SQLite mode unavailable")
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.rollback()
                raise
            try:
                db.commit()
                # EXTRA syncs the rollback-journal unlink. Explicitly sync the
                # parent too, before returning ANY committed intent/outcome.
                # Constructor-only directory sync cannot cover this unlink.
                directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except Exception as error:
                raise CompactionJournalUnknownError(
                    "Compaction journal durability UNKNOWN; never dispatch or replay"
                ) from error
        finally:
            db.close()

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
            with self._transaction() as db:
                selected = self._blocking_selected_summary(db, canonical, inputs)
                if selected and (
                    len(selected) != 1
                    or not selected[0].state.reservable_commit
                    or type(intent) is not dict
                    or intent.get("selectedSummaryOperationId") != selected[0].operation_id
                ):
                    raise CompactionJournalError(
                        "Blocked selected summary; unrelated native commit forbidden"
                    )
                db.execute(
                    "INSERT INTO operations VALUES (?, ?, ?, ?, NULL)",
                    (commit_id, canonical, payload, IntentOperation.declared_name),
                )
        except sqlite3.IntegrityError as error:
            raise CompactionJournalError(
                "Unresolved session or reused commit ID; never replay"
            ) from error
        return commit_id

    def get(self, commit_id: str) -> CompactionOperation:
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM operations WHERE commit_id = ?", (commit_id,)
            ).fetchone()
        if row is None:
            raise CompactionJournalError("Unknown compaction operation")
        return CompactionOperation.from_row(row)

    def unresolved(self, session_file: str) -> tuple[CompactionOperation, ...]:
        """Discover crash-orphaned intents for explicit recovery, never dispatch."""
        canonical = str(Path(session_file).resolve(strict=True))
        with self._transaction() as db:
            rows = db.execute(
                "SELECT * FROM operations WHERE session_file = ? "
                f"AND status IN {sql_names(OperationState, unresolved=True)}",
                (canonical,),
            ).fetchall()
        return tuple(CompactionOperation.from_row(row) for row in rows)

    def enroll_fresh_private_session(
        self,
        fresh: FreshPrivateSession,
        *,
        owner_name: str,
        owner_created_at: str,
        owner_lookup: str,
        owner_generation: int,
        admission_generation: int,
    ) -> None:
        """Persist exact O_EXCL new-session coverage under the caller's owner locks.

        A file/header alone cannot be enrolled later: ``fresh`` is a returned
        process-local creation object, and this transaction's COMMIT + parent
        fsync must return before any native prompt. The caller must first
        verify its current registry owner/SQL generation under the shared wire
        lock. This row is NOT terminal raw-input settlement or selected grant.
        """
        from .fresh_private_session import FreshPrivateSession

        if type(fresh) is not FreshPrivateSession:
            raise CompactionJournalError("Returned fresh-session creation required")
        fresh.verify_prewrite()
        if type(owner_lookup) is not str or not owner_lookup or "/" in owner_lookup:
            raise CompactionJournalError("Exact private owner lookup required")
        private_root = (self.path.parent / "native-sessions").resolve(strict=False)
        if (
            fresh.path.resolve(strict=True).parent != private_root / owner_lookup
            or type(owner_name) is not str
            or not owner_name
            or type(owner_created_at) is not str
            or not owner_created_at
            or type(owner_generation) is not int
            or owner_generation <= 0
            or type(admission_generation) is not int
            or admission_generation <= 0
        ):
            raise CompactionJournalError("Fresh-session owner or private location differs")
        try:
            with self._transaction() as db:
                if (
                    db.execute(
                        "SELECT 1 FROM private_raw_inputs WHERE session_file = ? LIMIT 1",
                        (str(fresh.path),),
                    ).fetchone()
                    or db.execute(
                        "SELECT 1 FROM selected_summary_attempts WHERE session_file = ? LIMIT 1",
                        (str(fresh.path),),
                    ).fetchone()
                    or db.execute(
                        "SELECT 1 FROM operations WHERE session_file = ? LIMIT 1",
                        (str(fresh.path),),
                    ).fetchone()
                ):
                    raise CompactionJournalError("Fresh-session history already exists")
                fresh.verify_prewrite()
                db.execute(
                    "INSERT INTO enrolled_private_sessions "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(fresh.path),
                        fresh.session_id,
                        fresh.device,
                        fresh.inode,
                        fresh.header_sha256,
                        owner_name,
                        owner_created_at,
                        owner_lookup,
                        owner_generation,
                        admission_generation,
                        fresh.creator_pid,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise CompactionJournalError("Fresh-session coverage already enrolled") from error
        _returned_fresh_enrollments[fresh] = (
            str(self.path),
            fresh.session_id,
            fresh.device,
            fresh.inode,
            fresh.header_sha256,
            owner_name,
            owner_created_at,
            owner_lookup,
            owner_generation,
            admission_generation,
            fresh.creator_pid,
        )

    def reserve_selected_summary(
        self,
        session_file: str,
        source: dict,
        *,
        operation_id: str | None = None,
        fresh_session: FreshPrivateSession | None = None,
        admission_generation: int | None = None,
    ) -> str:
        """Durably reserve BEFORE any selected Pi RPC send or auth side effect.

        All selected attempts, including terminal-looking ones, remain blocking
        until separately reviewed exact-ID recovery exists. A failure before
        writing stdin does not authorize retry.
        The caller must separately retain owner/turn/ingress authority; this
        journal is only an exclusion and recovery record, never a bearer grant.
        """
        operation_id = uuid4().hex if operation_id is None else operation_id
        if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
            raise ValueError("Expected exact selected summary operation ID")
        canonical = str(Path(session_file).resolve(strict=True))
        info = Path(canonical).lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            # An outward hardlink can alias a private saved inode while its
            # lexical path evades the private-root floor. Never reserve on a
            # multiply linked inode, regardless of the supplied path.
            raise CompactionJournalError("Selected saved session must have one private inode link")
        private_sessions = (self.path.parent / "native-sessions").resolve(strict=False)
        private = Path(canonical).is_relative_to(private_sessions)
        if private and fresh_session is not None:
            # Never infer coverage from a visible file, missing marker, or an
            # enrolment SQL row alone after an uncertain fsync/restart. The
            # original O_EXCL creation object must still be in this process.
            from .fresh_private_session import FreshPrivateSession

            if type(fresh_session) is not FreshPrivateSession:
                raise CompactionJournalError(
                    "Private selected reservation requires reviewed raw-history coverage floor"
                )
            fresh_session.verify_saved_identity()
            if fresh_session.path != Path(canonical) or (
                admission_generation is not None and type(admission_generation) is not int
            ):
                raise CompactionJournalError("Fresh private selected identity changed")
        if (
            type(source) is not dict
            or set(source) != {"source", "selected", "settings"}
            or any(type(value) is not dict or not value for value in source.values())
        ):
            raise ValueError("Source, selected route and settings witnesses required")
        payload = json.dumps(source, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(payload.encode()) > 65536:
            raise ValueError("Selected summary source exceeds bound")
        if "reservedRevision" in source["source"]:
            from .backend import _session_revision

            revision = _session_revision(canonical)
            if revision is None or source["source"]["reservedRevision"] != json.loads(
                json.dumps(revision)
            ):
                raise ValueError("Selected summary saved source revision changed")
        if "originalSha256" in source["source"]:
            witness = source["source"]
            key = witness.get("ingressKey")
            row = (
                InputDispositions(self.path.parent / InputDispositions.filename)
                .read()
                .rows.get(key)
                if type(key) is str
                else None
            )
            if (
                row is None
                or not row.unresolved
                or row.native_id is not None
                or row.owner != witness.get("ownerName")
                or row.admission != witness.get("admissionGeneration")
                or hashlib.sha256(row.source_text.encode()).hexdigest() != witness["originalSha256"]
            ):
                raise ValueError("Selected summary durable original input changed")
        try:
            with (
                InputDispositions(
                    self.path.parent / InputDispositions.filename
                ).reading() as inputs,
                self._transaction() as db,
            ):
                if private and fresh_session is not None:
                    assert fresh_session is not None
                    coverage = db.execute(
                        "SELECT session_id,device,inode,header_sha256,owner_name,"
                        "owner_created_at,owner_lookup,owner_generation,"
                        "admission_epoch,creator_pid "
                        "FROM enrolled_private_sessions WHERE session_file = ?",
                        (canonical,),
                    ).fetchone()
                    witness = source["source"]
                    if (
                        coverage is None
                        or _returned_fresh_enrollments.get(fresh_session)
                        != (str(self.path), *coverage)
                        or witness.get("ownerName") != coverage[4]
                        or witness.get("ownerCreatedAt") != coverage[5]
                        or Path(canonical).parent.name != coverage[6]
                        or witness.get("ownerPid") != coverage[9]
                        or (
                            admission_generation is not None and admission_generation != coverage[8]
                        )
                    ):
                        raise CompactionJournalError("Fresh private owner coverage differs")
                    fresh_session.verify_saved_identity()
                raw_ids = frozenset(
                    row[0]
                    for row in db.execute(
                        "SELECT input_id FROM private_raw_inputs WHERE session_file = ?",
                        (canonical,),
                    )
                )
                if private and fresh_session is None:
                    from .continued_private_session import verify_continued_private_session

                    try:
                        verify_continued_private_session(
                            self.path.parent, Path(canonical), source["source"], raw_ids
                        )
                    except (OSError, ValueError, sqlite3.Error, RuntimeError) as error:
                        raise CompactionJournalError(
                            "Private selected reservation requires reviewed "
                            "raw-history coverage floor"
                        ) from error
                if db.execute(
                    "SELECT 1 FROM operations WHERE session_file = ? "
                    f"AND status IN {sql_names(OperationState, unresolved=True)} LIMIT 1",
                    (canonical,),
                ).fetchone():
                    raise CompactionJournalError("Unresolved native commit; no selected summary")
                if self._blocking_selected_summary(db, canonical, inputs) or (
                    raw_ids and (not private or fresh_session is not None)
                ):
                    raise CompactionJournalError("Blocked selected summary; never replay")
                db.execute(
                    "INSERT INTO selected_summary_attempts VALUES (?, ?, ?, ?, NULL, NULL)",
                    (operation_id, canonical, payload, ReservedSummary.declared_name),
                )
        except sqlite3.IntegrityError as error:
            raise CompactionJournalError(
                "Blocked selected summary or reused operation ID; never replay"
            ) from error
        return operation_id

    def selected_summary(self, operation_id: str) -> SelectedSummaryAttempt:
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM selected_summary_attempts WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        if row is None:
            raise CompactionJournalError("Unknown selected summary operation")
        return SelectedSummaryAttempt.from_row(row)

    def unresolved_selected_summary(self, session_file: str) -> tuple[SelectedSummaryAttempt, ...]:
        """Crash-orphaned reservations block every subsequent input send."""
        canonical = str(Path(session_file).resolve(strict=True))
        with self._transaction() as db:
            rows = db.execute(
                "SELECT * FROM selected_summary_attempts WHERE session_file = ? "
                f"AND status IN {sql_names(SummaryState, unresolved=True)}",
                (canonical,),
            ).fetchall()
        return tuple(SelectedSummaryAttempt.from_row(row) for row in rows)

    def _blocking_selected_summary(
        self, db: sqlite3.Connection, canonical: str, inputs: InputDocument
    ) -> tuple[SelectedSummaryAttempt, ...]:
        rows = db.execute(
            "SELECT * FROM selected_summary_attempts WHERE session_file = ?", (canonical,)
        ).fetchall()
        if not rows:
            return ()
        return tuple(
            attempt
            for row in rows
            if not (attempt := SelectedSummaryAttempt.from_row(row)).original_has_started(
                inputs.rows
            )
        )

    def blocking_selected_summary(self, session_file: str) -> tuple[SelectedSummaryAttempt, ...]:
        """Block until the exact original input has durable native-start evidence.

        Terminal-looking summary rows never grant a send or mint a token. Once
        the original has actually started, the existing disposition prevents
        replay while ordinary new inputs and later compactions can proceed.
        """
        canonical = str(Path(session_file).resolve(strict=True))
        with (
            InputDispositions(self.path.parent / InputDispositions.filename).reading() as inputs,
            self._transaction() as db,
        ):
            return self._blocking_selected_summary(db, canonical, inputs)

    def reserve_private_raw_input(self, session_file: Path, input_id: str) -> None:
        """Durably mark exact private-session raw input UNKNOWN before any pipe write.

        Called after PR94's owner/claim/prompt binding checks under its shared
        wire lock. The marker lives in the SAME journal as selected reservation,
        so their write transactions serialize. This returned fsync ACK permits
        only attempting the one raw write; it never proves provider acceptance
        or authorizes later selected summary reservation.
        """
        if type(input_id) is not str or not re.fullmatch(r"[0-9a-f]{32}", input_id):
            raise CompactionJournalError("Exact private raw input ID required")
        canonical = str(session_file.resolve(strict=False))
        try:
            with (
                InputDispositions(
                    self.path.parent / InputDispositions.filename
                ).reading() as inputs,
                self._transaction() as db,
            ):
                if db.execute(
                    "SELECT 1 FROM operations WHERE session_file = ? "
                    f"AND status IN {sql_names(OperationState, unresolved=True)} LIMIT 1",
                    (canonical,),
                ).fetchone() or self._blocking_selected_summary(db, canonical, inputs):
                    raise CompactionJournalError(
                        "Selected or unresolved journal blocks native input"
                    )
                db.execute(
                    "INSERT INTO private_raw_inputs VALUES (?, ?, 'unknown')",
                    (input_id, canonical),
                )
        except sqlite3.IntegrityError as error:
            raise CompactionJournalError("Private raw input ID already reserved") from error

    @contextmanager
    def ordinary_input_send_fence(
        self, session_file: Path, *, private_input_id: str | None = None
    ) -> Iterator[None]:
        """Exclude selected rows and unresolved commits through a raw stdin write.

        The caller must already hold the authoritative shared wire lock. This
        journal BEGIN IMMEDIATE serializes even a concurrent direct reservation
        that did not take the wire lock; an incomplete selected attempt denies
        before bytes can enter the native pipe. The exact saved file, not a
        recipient-wide prefix or a post-send cursor, is the exclusion key.
        """
        # A fresh Pi get_state may name its future .jsonl before writing a
        # header. A reservation requires an existing file; the journal lock
        # excludes a newly created/reserved file through the raw write too.
        canonical = str(session_file.resolve(strict=False))
        private_sessions = (self.path.parent / "native-sessions").resolve(strict=False)
        if Path(canonical).is_relative_to(private_sessions) and private_input_id is None:
            raise CompactionJournalError("Private raw send requires durable prewrite marker")
        with (
            InputDispositions(self.path.parent / InputDispositions.filename).reading() as inputs,
            self._transaction() as db,
        ):
            if db.execute(
                "SELECT 1 FROM operations WHERE session_file = ? "
                f"AND status IN {sql_names(OperationState, unresolved=True)} LIMIT 1",
                (canonical,),
            ).fetchone() or self._blocking_selected_summary(db, canonical, inputs):
                raise CompactionJournalError("Selected or unresolved journal blocks native input")
            if private_input_id is not None and db.execute(
                "SELECT session_file,status FROM private_raw_inputs WHERE input_id = ?",
                (private_input_id,),
            ).fetchone() != (canonical, "unknown"):
                raise CompactionJournalError("Exact durable private raw prewrite marker required")
            yield

    def refuse_selected_summary(self, operation_id: str, reason: str) -> None:
        """Retain the observed native prestart failure without admitting any input."""
        target = RefusedSummary(reason)
        with self._transaction() as db:
            row = db.execute(
                "SELECT status, commit_id, decline_reason FROM selected_summary_attempts "
                "WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None or not SummaryState.from_columns(*row).may_become(target):
                raise CompactionJournalError("Selected summary refusal transition forbidden")
            db.execute(
                "UPDATE selected_summary_attempts SET status = ?, decline_reason = ? "
                "WHERE operation_id = ?",
                (target.declared_name, target.decline_reason, operation_id),
            )

    def selected_summaries(self, session_file: str) -> tuple[SelectedSummaryAttempt, ...]:
        """Inspect every recorded result without exposing model or input content."""
        canonical = str(Path(session_file).resolve(strict=True))
        with self._transaction() as db:
            rows = db.execute(
                "SELECT * FROM selected_summary_attempts WHERE session_file = ? ORDER BY rowid",
                (canonical,),
            ).fetchall()
        return tuple(SelectedSummaryAttempt.from_row(row) for row in rows)

    def mark_selected_summary_unknown(self, operation_id: str) -> None:
        """Record transport uncertainty; never erase or retry the reservation."""
        with self._transaction() as db:
            row = db.execute(
                "SELECT status, commit_id, decline_reason FROM selected_summary_attempts WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None or not SummaryState.from_columns(*row).may_become(UnknownSummary()):
                raise CompactionJournalError("Selected summary uncertainty transition forbidden")
            db.execute(
                "UPDATE selected_summary_attempts SET status = ? WHERE operation_id = ?",
                (UnknownSummary.declared_name, operation_id),
            )

    def decline_selected_summary_prestart(
        self,
        operation_id: str,
        reason: str,
        *,
        admission: SelectedAdmissionIdentity | None = None,
    ) -> SelectedSummaryAdmission | None:
        """Settle only an exact, verified clean Pi response before any side effect.

        Only split-turn or explicitly unsupported cuts may be *recorded* as
        clean pre-start declines. Even these remain blocked at the final input
        gate until separately reviewed exact-ID recovery exists. Busy, changed
        source/model/settings, timeout, transport loss, and post-auth/stream
        errors remain blocking.
        The future owner caller must verify the correlated Pi reply and current
        owner/ingress source before invoking this method; the journal is not
        that authority or evidence verifier.
        """
        try:
            target = DeclinedPrestartSummary(reason)
        except ValueError as error:
            raise CompactionJournalError(str(error)) from error
        with self._transaction() as db:
            row = db.execute(
                "SELECT session_file, source_json, status, commit_id, decline_reason "
                "FROM selected_summary_attempts WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None or not SummaryState.from_columns(*row[2:]).may_become(target):
                raise CompactionJournalError("Selected summary prestart decline forbidden")
            before = db.total_changes
            db.execute(
                "UPDATE selected_summary_attempts SET status = ?, "
                "decline_reason = ? WHERE operation_id = ? AND status = ?",
                (target.declared_name, reason, operation_id, ReservedSummary.declared_name),
            )
            after = db.execute(
                "SELECT session_file, source_json, status, commit_id, decline_reason "
                "FROM selected_summary_attempts WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if db.total_changes != before + 1 or after != (
                row[0],
                row[1],
                target.declared_name,
                None,
                reason,
            ):
                raise CompactionJournalError("Exact clean decline transition required")
        if admission is not None:
            from .selected_summary_admission import SelectedSummaryAdmission

            # This method's verified clean-decline SQL is the only issuer.
            # _transaction() has already returned COMMIT + parent-fsync ACK.
            scope = (str(self.path), row[0], operation_id, target, row[1])
            receipt = _ReturnedTerminalAck()
            _issued_selected_acks[receipt] = scope
            return SelectedSummaryAdmission._from_returned_ack(
                receipt,
                self.path,
                row[0],
                operation_id,
                target,
                row[1],
                admission,
            )
        return None

    def link_selected_summary_commit(
        self,
        operation_id: str,
        commit_id: str,
        *,
        admission: SelectedAdmissionIdentity | None = None,
    ) -> SelectedSummaryAdmission | None:
        """Settle only a reserved attempt after its exact native commit is durable.

        This is NOT a provider receipt validator or an input admission grant.
        A linked row remains a blocker until its exact original input has
        native-start evidence. The owner checks the complete selected Pi result
        and current owner/ingress source before the native CAS, and calls this
        only after the native journal committed.
        An UNKNOWN provider attempt cannot be automatically linked or retried.
        """
        target = LinkedSummary(commit_id)
        with self._transaction() as db:
            row = db.execute(
                "SELECT session_file, status, source_json, commit_id, decline_reason "
                "FROM selected_summary_attempts WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            commit = db.execute(
                "SELECT session_file, status, intent_json FROM operations WHERE commit_id = ?",
                (commit_id,),
            ).fetchone()
            try:
                bound = commit is not None and (
                    json.loads(commit[2]).get("selectedSummaryOperationId") == operation_id
                )
            except (TypeError, ValueError, AttributeError):
                bound = False
            if (
                row is None
                or not SummaryState.from_columns(row[1], *row[3:]).may_become(target)
                or commit is None
                or commit[0] != row[0]
                or not OperationState.decode(commit[1]).committed
                or not bound
            ):
                raise CompactionJournalError("Exact committed native result required to link")
            if (
                admission is not None
                and json.loads(commit[2]).get("selectedSummarySourceDigest")
                != hashlib.sha256(row[2].encode()).hexdigest()
            ):
                raise CompactionJournalError("Selected native intent source digest required")
            before = db.total_changes
            db.execute(
                "UPDATE selected_summary_attempts SET status = ?, commit_id = ? "
                "WHERE operation_id = ? AND status = ?",
                (target.declared_name, commit_id, operation_id, ReservedSummary.declared_name),
            )
            after = db.execute(
                "SELECT session_file, status, source_json, commit_id, decline_reason "
                "FROM selected_summary_attempts WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if db.total_changes != before + 1 or after != (
                row[0],
                target.declared_name,
                row[2],
                commit_id,
                None,
            ):
                raise CompactionJournalError("Exact committed native link required")
        if admission is not None:
            from .selected_summary_admission import SelectedSummaryAdmission

            # Only this method's verified native-link SQL can mint on returned fsync.
            scope = (str(self.path), row[0], operation_id, target, row[2])
            receipt = _ReturnedTerminalAck()
            _issued_selected_acks[receipt] = scope
            return SelectedSummaryAdmission._from_returned_ack(
                receipt,
                self.path,
                row[0],
                operation_id,
                target,
                row[2],
                admission,
            )
        return None

    def pending_publications(self, session_file: str) -> tuple[CompactionPublication, ...]:
        """Read exact-ID metadata; an unknown commit cannot be projected."""
        canonical = str(Path(session_file).resolve(strict=True))
        with self._transaction() as db:
            rows = db.execute(
                "SELECT p.commit_id, p.session_file, p.metadata_json, p.status, "
                "o.evidence_json, o.session_file "
                "FROM publications p JOIN operations o ON o.commit_id = p.commit_id "
                "WHERE p.session_file = ? AND p.status = ? AND o.status = ? "
                "ORDER BY p.rowid LIMIT 32",
                (canonical, PendingPublication.declared_name, CommittedOperation.declared_name),
            ).fetchall()
        publications = []
        for commit_id, file, metadata, status, evidence_json, owner_file in rows:
            try:
                if (
                    file != owner_file
                    or len(metadata.encode()) > 4096
                    or metadata != _publication_metadata(commit_id, json.loads(evidence_json))
                ):
                    raise ValueError("Changed publication")
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                raise CompactionJournalError("Compaction publication metadata changed") from error
            publications.append(
                CompactionPublication(commit_id, file, metadata, PublicationState.decode(status)())
            )
        return tuple(publications)

    def observe_publication(self, commit_id: str, metadata_json: str) -> None:
        """ACK only the exact metadata seen by the local ACP projection.

        Delivery must return before this call. A failure before marking leaves
        the row pending. A post-COMMIT fsync error may leave it *observed* even
        though this call raises UNKNOWN; reconcile the exact row, never infer
        native retry authority. Reprojection may repeat a pending commit ID, so
        consumers must deduplicate by ID, never by summary text.
        """
        with self._transaction() as db:
            row = db.execute(
                "SELECT metadata_json, status FROM publications WHERE commit_id = ?", (commit_id,)
            ).fetchone()
            if row is None or row[0] != metadata_json:
                raise CompactionJournalError("Unknown or changed publication metadata")
            if PublicationState.decode(row[1])().may_become(ObservedPublication()):
                db.execute(
                    "UPDATE publications SET status = ? WHERE commit_id = ?",
                    (ObservedPublication.declared_name, commit_id),
                )

    def resolve(
        self, commit_id: str, outcome: OperationState, evidence: dict, *, publication: bool = False
    ) -> None:
        """Persist bridge-validated native evidence; this does not verify it.

        Unknown/intent remains blocking. A terminal outcome is immutable.
        ``refused`` is only allowed directly after intent, for a proven
        pre-write native refusal. Once UNKNOWN, only writer-fenced exact-ID
        reconciliation may establish committed or aborted-no-write.
        """
        metadata = _publication_metadata(commit_id, evidence) if publication else None
        if publication and not outcome.committed:
            raise CompactionJournalError("Exact committed native metadata required")
        payload = json.dumps(evidence, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(payload.encode()) > 65536:
            raise ValueError("Compaction outcome exceeds bound")
        with self._transaction() as db:
            row = db.execute(
                "SELECT status, session_file FROM operations WHERE commit_id = ?", (commit_id,)
            ).fetchone()
            if row is None or not OperationState.decode(row[0])().may_become(outcome):
                raise CompactionJournalError("Compaction outcome transition forbidden")
            db.execute(
                "UPDATE operations SET status = ?, evidence_json = ? WHERE commit_id = ?",
                (outcome.declared_name, payload, commit_id),
            )
            if metadata is not None:
                db.execute(
                    "INSERT INTO publications VALUES (?, ?, ?, ?)",
                    (commit_id, row[1], metadata, PendingPublication.declared_name),
                )
