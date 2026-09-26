"""Durable, non-replayable compaction intents (not an authority mechanism).

The owner commit bridge must call this inside its registry authority scope.
Native reconciliation evidence must be collected under the native writer lock
before calling ``resolve``. No method here dispatches or retries native work.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

Outcome = Literal["committed", "refused", "aborted-no-write", "unknown"]
_TERMINAL = frozenset({"committed", "refused", "aborted-no-write"})


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
class CompactionPublication:
    """Local metadata-only projection, keyed by native commit ID; no recipient."""

    commit_id: str
    session_file: str
    metadata_json: str
    status: str


def _publication_metadata(commit_id: str, evidence: dict) -> str:
    if (
        set(evidence) != {"status", "entryId", "revision", "leafId"}
        or evidence.get("status") != "committed"
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
        parent = path.parent.resolve(strict=True)
        for directory in (parent, *parent.parents):
            parent_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
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
