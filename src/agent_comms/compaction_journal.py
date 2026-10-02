"""One durable database lifecycle shared by compaction transaction roles."""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from .compaction_errors import CompactionJournalError, CompactionJournalUnknownError
from .compaction_operations import NativeOperations
from .compaction_private_inputs import PrivateInputs
from .compaction_publications import CompactionPublications
from .compaction_records import (JournalMode, JournalSchemaObject, JournalTable,
                                 SelectedSummaryAttempt, SyncMode)
from .field_codec import FieldCodec
from .compaction_identity import JournalCustody
from .compaction_summaries import SelectedSummaries
from .typed_table import TypedTable
from .thread_identity import ThreadIncarnation
from .registry_provenance import RegistryProvenance


class CompactionJournal:
    @classmethod
    def snapshot(cls, path: Path, session_file: str, incarnation: ThreadIncarnation,
                 registry: RegistryProvenance):
        """Observe the original journal without the writer's creation/durability path.

        mode=ro respects commits; immutable=1 would conceal genuine transitions.
        This current journal uses rollback mode. Reject WAL before SQLite opens
        it so even a read cannot create a shared-memory sidecar. No absent file
        or schema is initialized, and no admission/durability receipt is minted.
        """
        from .compaction_outcomes import CompactionOutcomeSnapshot

        return cls.observe_readonly(path, lambda db: CompactionOutcomeSnapshot.read(
            db, session_file, incarnation, registry), absent=CompactionOutcomeSnapshot(()))

    @classmethod
    def observe_readonly(cls, path: Path, observe: Callable, *, absent):
        """One read lifetime for original journal consumers; no writer setup.

        All consumers share the custody, rollback-format and transaction checks
        below. Missing storage is observed without creating a database or sidecar.
        """

        try:
            initial = path.lstat()
        except FileNotFoundError:
            return absent
        if not stat.S_ISREG(initial.st_mode) or initial.st_nlink != 1:
            raise CompactionJournalError("Journal must be a regular private file")
        try:
            with path.open("rb") as stream:
                info = os.fstat(stream.fileno())
                if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise CompactionJournalError("Journal must be a regular private file")
                header = stream.read(100)
        except FileNotFoundError as error:
            raise CompactionJournalError("Journal removed before read") from error
        if header[:16] != b"SQLite format 3\0" or header[18:20] != b"\x01\x01":
            raise CompactionJournalError("Read-only journal requires current rollback format")
        custody = JournalCustody.capture(path)
        if (custody.device, custody.inode) != (info.st_dev, info.st_ino) or (
            info.st_dev, info.st_ino
        ) != (initial.st_dev, initial.st_ino):
            raise CompactionJournalError("Journal replaced before read")
        db = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True,
                             isolation_level=None, timeout=0.25)
        try:
            db.execute("PRAGMA query_only=ON")
            if JournalMode.read(db.execute("PRAGMA journal_mode")) != [JournalMode("delete")]:
                raise CompactionJournalError("Read-only journal requires current rollback mode")
            db.execute("BEGIN")
            try:
                result = observe(db)
                if JournalCustody.capture(path) != custody:
                    raise CompactionJournalError("Journal replaced during read")
                return result
            finally:
                db.execute("ROLLBACK")
        finally:
            db.close()

    @classmethod
    def retained_history(cls, path: Path, session_file: str) -> dict[str, object]:
        """Read every declared journal role for this original saved-session path.

        Selected requests retain typed source facts. Native intents retain their
        original one-way source views and byte strings; inspection never decodes
        those views into a new source authority or an admission capability.
        """
        tables = TypedTable.members_with(JournalTable)
        empty = {table.declared_name: [] for table in tables}

        def read(db):
            return {table.declared_name: [row.inspection() for row in table.for_session(
                db, session_file)] for table in tables}

        return dict(session_file=session_file,
                    scope="original journal rows for this saved-session path; historical owners remain recorded",
                    tables=cls.observe_readonly(path, read, absent=empty))

    @classmethod
    def retained_changes(cls, path: Path, session_file: str) -> dict[str, object]:
        """Compare the last two original selected source cuts, not live context."""
        def read(db):
            attempts = SelectedSummaryAttempt.select(db,
                where="session_file=? ORDER BY rowid DESC LIMIT 2", parameters=(session_file,))
            if len(attempts) != 2:
                raise ValueError("Retained-source diff requires two original selected compaction attempts")
            current, previous = attempts
            return dict(scope="last two original selected compaction source cuts; not current native context",
                        previous=FieldCodec.encode(previous.identity),
                        current=FieldCodec.encode(current.identity),
                        **current.request.retained.changed_from(previous.request.retained))

        # A missing journal has the same precise contract as insufficient cuts.
        return cls.observe_readonly(path, read, absent={
            "scope": "last two original selected compaction source cuts; not current native context",
            "available": False,
            "reason": "No original compaction journal exists",
        })

    def __init__(self, path: Path):
        self.path = path
        self.operations = NativeOperations(self)
        self.summaries = SelectedSummaries(self)
        self.private_inputs = PrivateInputs(self)
        self.publications = CompactionPublications(self)
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
        with self.transaction() as db:
            tables = TypedTable.members_with(JournalTable)
            expected = {sql for table in tables for sql in table.ddl()}
            actual = {
                row.sql
                for row in JournalSchemaObject.read(
                    db.execute("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL")
                )
            }
            if not actual:
                for table in tables:
                    table.create(db)
            elif actual != expected:
                raise CompactionJournalError(
                    "Compaction journal schema differs; quiet migration required"
                )
        parent = path.parent.resolve(strict=True)
        for directory in (parent, *parent.parents):
            parent_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)

    @contextmanager
    def transaction(
        self, *, blocking: bool = True, retain_exclusion: bool = False
    ) -> Iterator[sqlite3.Connection]:
        """Durable generic transaction; never mints a selected admission ACK.

        Only the verified terminal methods below issue their one-use receipt,
        after this exact transaction's commit and parent fsync have returned.
        A caller's status-only SQL transition cannot use this generic API to
        construct input authority.
        """
        db = sqlite3.connect(self.path, timeout=5 if blocking else 0)
        try:
            if retain_exclusion:
                # SQLite retains its original EXCLUSIVE file lock across COMMIT
                # until this connection closes. No advisory lock or new store.
                db.execute("PRAGMA locking_mode=EXCLUSIVE")
            mode = JournalMode.read(db.execute("PRAGMA journal_mode=DELETE"))
            db.execute("PRAGMA synchronous=EXTRA")
            if mode != [JournalMode("delete")] or SyncMode.read(
                db.execute("PRAGMA synchronous")
            ) != [SyncMode(3)]:
                raise CompactionJournalError("Required durable SQLite mode unavailable")
            db.execute("BEGIN EXCLUSIVE" if retain_exclusion else "BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.rollback()
                raise
            self.commit(db)
        finally:
            db.close()

    def commit(self, db: sqlite3.Connection) -> None:
        """The same durable checkpoint for a transaction or retained raw fence."""
        try:
            db.commit()
            directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception as error:
            raise CompactionJournalUnknownError(
                "Compaction journal durability UNKNOWN; never dispatch or replay"
            ) from error
