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
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4
from weakref import WeakKeyDictionary

if TYPE_CHECKING:
    from .selected_summary_admission import SelectedAdmissionIdentity, SelectedSummaryAdmission

Outcome = Literal["committed", "refused", "aborted-no-write", "unknown"]
_TERMINAL = frozenset({"committed", "refused", "aborted-no-write"})


class _ReturnedTerminalAck:
    """Only the successful post-COMMIT parent fsync creates this identity."""

    __slots__ = ("__weakref__",)


_issued_selected_acks: WeakKeyDictionary[_ReturnedTerminalAck, tuple] = WeakKeyDictionary()


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
    status: str
    evidence_json: str | None


@dataclass(frozen=True)
class SelectedSummaryAttempt:
    """Provider attempt reservation, not a summary or native commit receipt."""

    operation_id: str
    session_file: str
    source_json: str
    status: str
    commit_id: str | None
    decline_reason: str | None


@dataclass(frozen=True)
class CompactionPublication:
    """Local metadata-only projection, keyed by native commit ID; no recipient."""

    commit_id: str
    session_file: str
    metadata_json: str
    status: str


def _publication_metadata(commit_id: str, evidence: dict) -> str:
    if (
        set(evidence)
        not in (
            {"status", "entryId", "revision", "leafId"},
            {"status", "entryId", "revision", "leafId", "metadataDigest"},
        )
        or evidence.get("status") != "committed"
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
            db.execute("""CREATE TABLE IF NOT EXISTS operations (
                    commit_id TEXT PRIMARY KEY,
                    session_file TEXT NOT NULL,
                    intent_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN
                        ('intent','unknown','committed','refused','aborted-no-write')),
                    evidence_json TEXT
                )""")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS unresolved_session
                ON operations(session_file) WHERE status IN ('intent','unknown')""")
            db.execute("""CREATE TABLE IF NOT EXISTS publications (
                    commit_id TEXT PRIMARY KEY REFERENCES operations(commit_id),
                    session_file TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','observed'))
                )""")
            db.execute("""CREATE TABLE IF NOT EXISTS selected_summary_attempts (
                    operation_id TEXT PRIMARY KEY,
                    session_file TEXT NOT NULL,
                    source_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN
                        ('reserved','unknown','linked','declined-prestart')),
                    commit_id TEXT,
                    decline_reason TEXT
                )""")
            # 47c8's partial index permitted several historical terminal rows.
            # Preserve every row: a global UNIQUE migration would fail journal
            # open and block unrelated sessions. BEGIN IMMEDIATE + the SELECT
            # before reserve below excludes any *new* attempt for that session.
            db.execute("DROP INDEX IF EXISTS selected_summary_session")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS unresolved_selected_summary_session
                ON selected_summary_attempts(session_file)
                WHERE status IN ('reserved','unknown')""")
            # Exact saved-session prewrite barrier for PR94's private raw writer.
            # An UNKNOWN marker is never cleared by a pipe ACK or fake result.
            db.execute("""CREATE TABLE IF NOT EXISTS private_raw_inputs (
                    input_id TEXT PRIMARY KEY,
                    session_file TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status = 'unknown')
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

    def begin(self, session_file: str, intent: dict, *, commit_id: str | None = None) -> str:
        """Durably reserve one operation. Every reused ID is forbidden forever."""
        commit_id = uuid4().hex if commit_id is None else commit_id
        if not re.fullmatch(r"[0-9a-f]{32}", commit_id):
            raise ValueError("Expected exact compaction commit ID")
        canonical = str(Path(session_file).resolve(strict=True))
        payload = json.dumps(intent, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(payload.encode()) > 65536:
            raise ValueError("Compaction intent exceeds bound")
        try:
            with self._transaction() as db:
                selected = db.execute(
                    "SELECT operation_id, status FROM selected_summary_attempts "
                    "WHERE session_file = ?",
                    (canonical,),
                ).fetchall()
                if selected and (
                    len(selected) != 1
                    or selected[0][1] != "reserved"
                    or type(intent) is not dict
                    or intent.get("selectedSummaryOperationId") != selected[0][0]
                ):
                    raise CompactionJournalError(
                        "Blocked selected summary; unrelated native commit forbidden"
                    )
                db.execute(
                    "INSERT INTO operations VALUES (?, ?, ?, 'intent', NULL)",
                    (commit_id, canonical, payload),
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
        return CompactionOperation(*row)

    def unresolved(self, session_file: str) -> tuple[CompactionOperation, ...]:
        """Discover crash-orphaned intents for explicit recovery, never dispatch."""
        canonical = str(Path(session_file).resolve(strict=True))
        with self._transaction() as db:
            rows = db.execute(
                "SELECT * FROM operations WHERE session_file = ? "
                "AND status IN ('intent','unknown')",
                (canonical,),
            ).fetchall()
        return tuple(CompactionOperation(*row) for row in rows)

    def reserve_selected_summary(
        self, session_file: str, source: dict, *, operation_id: str | None = None
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
        private_sessions = (self.path.parent / "native-sessions").resolve(strict=False)
        if Path(canonical).is_relative_to(private_sessions):
            # Existing PR94 sessions can predate this new raw-prewrite marker,
            # and the old runtime's pending input rows do not bind a saved
            # session path. No trustworthy migration/epoch floor exists yet.
            # Never infer clean history from missing marker rows or file times.
            raise CompactionJournalError(
                "Private selected reservation requires reviewed raw-history coverage floor"
            )
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
            from .input_disposition import InputDispositions

            witness = source["source"]
            key = witness.get("ingressKey")
            row = InputDispositions(self.path.parent).get(key) if type(key) is str else None
            if (
                row is None
                or row["status"] != "unknown"
                or row["native_id"] is not None
                or row["owner"] != witness.get("ownerName")
                or row["admission"] != witness.get("admissionGeneration")
                or type(row["source_text"]) is not str
                or hashlib.sha256(row["source_text"].encode()).hexdigest()
                != witness["originalSha256"]
            ):
                raise ValueError("Selected summary durable original input changed")
        try:
            with self._transaction() as db:
                if db.execute(
                    "SELECT 1 FROM operations WHERE session_file = ? "
                    "AND status IN ('intent','unknown') LIMIT 1",
                    (canonical,),
                ).fetchone():
                    raise CompactionJournalError("Unresolved native commit; no selected summary")
                if (
                    db.execute(
                        "SELECT 1 FROM selected_summary_attempts WHERE session_file = ? LIMIT 1",
                        (canonical,),
                    ).fetchone()
                    or db.execute(
                        "SELECT 1 FROM private_raw_inputs WHERE session_file = ? LIMIT 1",
                        (canonical,),
                    ).fetchone()
                ):
                    raise CompactionJournalError("Blocked selected summary; never replay")
                db.execute(
                    "INSERT INTO selected_summary_attempts "
                    "VALUES (?, ?, ?, 'reserved', NULL, NULL)",
                    (operation_id, canonical, payload),
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
        return SelectedSummaryAttempt(*row)

    def unresolved_selected_summary(self, session_file: str) -> tuple[SelectedSummaryAttempt, ...]:
        """Crash-orphaned reservations block every subsequent input send."""
        canonical = str(Path(session_file).resolve(strict=True))
        with self._transaction() as db:
            rows = db.execute(
                "SELECT * FROM selected_summary_attempts WHERE session_file = ? "
                "AND status IN ('reserved','unknown')",
                (canonical,),
            ).fetchall()
        return tuple(SelectedSummaryAttempt(*row) for row in rows)

    def blocking_selected_summary(self, session_file: str) -> tuple[SelectedSummaryAttempt, ...]:
        """All attempts block automatic native input, including apparent terminals.

        A post-COMMIT parent-fsync failure can leave linked or declined-prestart
        visible even though its caller received UNKNOWN. Neither status is
        automatic send authority, in this process or after a restart.
        """
        canonical = str(Path(session_file).resolve(strict=True))
        with self._transaction() as db:
            rows = db.execute(
                "SELECT * FROM selected_summary_attempts WHERE session_file = ?",
                (canonical,),
            ).fetchall()
        return tuple(SelectedSummaryAttempt(*row) for row in rows)

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
            with self._transaction() as db:
                if (
                    db.execute(
                        "SELECT 1 FROM operations WHERE session_file = ? "
                        "AND status IN ('intent','unknown') LIMIT 1",
                        (canonical,),
                    ).fetchone()
                    or db.execute(
                        "SELECT 1 FROM selected_summary_attempts WHERE session_file = ? LIMIT 1",
                        (canonical,),
                    ).fetchone()
                ):
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
        that did not take the wire lock; a selected row from any status denies
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
        with self._transaction() as db:
            if (
                db.execute(
                    "SELECT 1 FROM operations WHERE session_file = ? "
                    "AND status IN ('intent','unknown') LIMIT 1",
                    (canonical,),
                ).fetchone()
                or db.execute(
                    "SELECT 1 FROM selected_summary_attempts WHERE session_file = ? LIMIT 1",
                    (canonical,),
                ).fetchone()
            ):
                raise CompactionJournalError("Selected or unresolved journal blocks native input")
            if private_input_id is not None and db.execute(
                "SELECT session_file,status FROM private_raw_inputs WHERE input_id = ?",
                (private_input_id,),
            ).fetchone() != (canonical, "unknown"):
                raise CompactionJournalError("Exact durable private raw prewrite marker required")
            yield

    def mark_selected_summary_unknown(self, operation_id: str) -> None:
        """Record transport uncertainty; never erase or retry the reservation."""
        with self._transaction() as db:
            row = db.execute(
                "SELECT status FROM selected_summary_attempts WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None or row[0] not in {"reserved", "unknown"}:
                raise CompactionJournalError("Selected summary uncertainty transition forbidden")
            db.execute(
                "UPDATE selected_summary_attempts SET status = 'unknown' WHERE operation_id = ?",
                (operation_id,),
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
        if type(reason) is not str or reason not in {"split_turn", "unsupported"}:
            raise CompactionJournalError("Selected summary decline is not a clean skip")
        with self._transaction() as db:
            row = db.execute(
                "SELECT session_file, source_json, status, commit_id, decline_reason "
                "FROM selected_summary_attempts WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None or row[2:] != ("reserved", None, None):
                raise CompactionJournalError("Selected summary prestart decline forbidden")
            before = db.total_changes
            db.execute(
                "UPDATE selected_summary_attempts SET status = 'declined-prestart', "
                "decline_reason = ? WHERE operation_id = ? AND status = 'reserved'",
                (reason, operation_id),
            )
            after = db.execute(
                "SELECT session_file, source_json, status, commit_id, decline_reason "
                "FROM selected_summary_attempts WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if db.total_changes != before + 1 or after != (
                row[0],
                row[1],
                "declined-prestart",
                None,
                reason,
            ):
                raise CompactionJournalError("Exact clean decline transition required")
        if admission is not None:
            from .selected_summary_admission import SelectedSummaryAdmission

            # This method's verified clean-decline SQL is the only issuer.
            # _transaction() has already returned COMMIT + parent-fsync ACK.
            scope = (str(self.path), row[0], operation_id, "declined-prestart", None, row[1])
            receipt = _ReturnedTerminalAck()
            _issued_selected_acks[receipt] = scope
            return SelectedSummaryAdmission._from_returned_ack(
                receipt,
                self.path,
                row[0],
                operation_id,
                "declined-prestart",
                None,
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
        Even a linked row remains a durable blocker until an exact-ID recovery
        path exists. A future caller must check the complete selected Pi result
        and current owner/ingress source before the native CAS, and call this
        only after the native journal committed.
        An UNKNOWN provider attempt cannot be automatically linked or retried.
        """
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
                or row[1] != "reserved"
                or row[3:] != (None, None)
                or commit is None
                or commit[:2] != (row[0], "committed")
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
                "UPDATE selected_summary_attempts SET status = 'linked', commit_id = ? "
                "WHERE operation_id = ? AND status = 'reserved'",
                (commit_id, operation_id),
            )
            after = db.execute(
                "SELECT session_file, status, source_json, commit_id, decline_reason "
                "FROM selected_summary_attempts WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if db.total_changes != before + 1 or after != (
                row[0],
                "linked",
                row[2],
                commit_id,
                None,
            ):
                raise CompactionJournalError("Exact committed native link required")
        if admission is not None:
            from .selected_summary_admission import SelectedSummaryAdmission

            # Only this method's verified native-link SQL can mint on returned fsync.
            scope = (str(self.path), row[0], operation_id, "linked", commit_id, row[2])
            receipt = _ReturnedTerminalAck()
            _issued_selected_acks[receipt] = scope
            return SelectedSummaryAdmission._from_returned_ack(
                receipt,
                self.path,
                row[0],
                operation_id,
                "linked",
                commit_id,
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
                "WHERE p.session_file = ? AND p.status = 'pending' AND o.status = 'committed' "
                "ORDER BY p.rowid LIMIT 32",
                (canonical,),
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
            publications.append(CompactionPublication(commit_id, file, metadata, status))
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
            if row[1] == "pending":
                db.execute(
                    "UPDATE publications SET status = 'observed' WHERE commit_id = ?",
                    (commit_id,),
                )

    def resolve(
        self, commit_id: str, outcome: Outcome, evidence: dict, *, publication: bool = False
    ) -> None:
        """Persist bridge-validated native evidence; this does not verify it.

        Unknown/intent remains blocking. A terminal outcome is immutable.
        ``refused`` is only allowed directly after intent, for a proven
        pre-write native refusal. Once UNKNOWN, only writer-fenced exact-ID
        reconciliation may establish committed or aborted-no-write.
        """
        if outcome not in _TERMINAL | {"unknown"}:
            raise ValueError("Invalid compaction outcome")
        metadata = _publication_metadata(commit_id, evidence) if publication else None
        if publication and outcome != "committed":
            raise CompactionJournalError("Exact committed native metadata required")
        payload = json.dumps(evidence, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(payload.encode()) > 65536:
            raise ValueError("Compaction outcome exceeds bound")
        with self._transaction() as db:
            row = db.execute(
                "SELECT status, session_file FROM operations WHERE commit_id = ?", (commit_id,)
            ).fetchone()
            if row is None or row[0] in _TERMINAL or (row[0] == "unknown" and outcome == "refused"):
                raise CompactionJournalError("Compaction outcome transition forbidden")
            db.execute(
                "UPDATE operations SET status = ?, evidence_json = ? WHERE commit_id = ?",
                (outcome, payload, commit_id),
            )
            if metadata is not None:
                db.execute(
                    "INSERT INTO publications VALUES (?, ?, ?, 'pending')",
                    (commit_id, row[1], metadata),
                )
