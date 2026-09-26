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


@dataclass(frozen=True)
class CompactionOperation:
    commit_id: str
    session_file: str
    intent_json: str
    status: str
    evidence_json: str | None


class CompactionJournal:
    """One durable journal per wire; at most one unresolved op per session.

    Parent directory must already exist (the registry owns its creation).
    SQLite FULL synchronous commits and parent-directory fsync make successful
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
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
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

    def resolve(self, commit_id: str, outcome: Outcome, evidence: dict) -> None:
        """Persist bridge-validated native evidence; this does not verify it.

        Unknown/intent remains blocking. A terminal outcome is immutable.
        ``refused`` is only allowed directly after intent, for a proven
        pre-write native refusal. Once UNKNOWN, only writer-fenced exact-ID
        reconciliation may establish committed or aborted-no-write.
        """
        if outcome not in _TERMINAL | {"unknown"}:
            raise ValueError("Invalid compaction outcome")
        payload = json.dumps(evidence, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(payload.encode()) > 65536:
            raise ValueError("Compaction outcome exceeds bound")
        with self._transaction() as db:
            row = db.execute(
                "SELECT status FROM operations WHERE commit_id = ?", (commit_id,)
            ).fetchone()
            if row is None or row[0] in _TERMINAL or (row[0] == "unknown" and outcome == "refused"):
                raise CompactionJournalError("Compaction outcome transition forbidden")
            db.execute(
                "UPDATE operations SET status = ?, evidence_json = ? WHERE commit_id = ?",
                (outcome, payload, commit_id),
            )
