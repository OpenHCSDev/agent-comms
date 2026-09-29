"""One durable database lifecycle shared by compaction transaction roles."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .compaction_errors import CompactionJournalError, CompactionJournalUnknownError
from .compaction_operations import NativeOperations
from .compaction_private_inputs import PrivateInputs
from .compaction_publications import CompactionPublications
from .compaction_records import JournalMode, JournalSchemaObject, JournalTable, SyncMode
from .compaction_summaries import SelectedSummaries
from .typed_table import TypedTable


class CompactionJournal:
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
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Durable generic transaction; never mints a selected admission ACK.

        Only the verified terminal methods below issue their one-use receipt,
        after this exact transaction's commit and parent fsync have returned.
        A caller's status-only SQL transition cannot use this generic API to
        construct input authority.
        """
        db = sqlite3.connect(self.path, timeout=5)
        try:
            mode = JournalMode.read(db.execute("PRAGMA journal_mode=DELETE"))
            db.execute("PRAGMA synchronous=EXTRA")
            if mode != [JournalMode("delete")] or SyncMode.read(
                db.execute("PRAGMA synchronous")
            ) != [SyncMode(3)]:
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
