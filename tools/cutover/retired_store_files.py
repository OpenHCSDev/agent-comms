"""Declarations of the superseded store files that carry_retired_stores.py deletes.

Delete with carry_retired_stores.py.
"""

from pathlib import Path

from agent_comms.locked_store import (
    DurableFile,
    PatternFile,
    PublishedSQLiteFile,
    SQLiteFile,
)


class RetiredRootFile(DurableFile, PatternFile):
    """An obsolete resource the retirement carry deletes."""

    @classmethod
    def retirement_paths(cls, path: Path) -> tuple[Path, ...]:
        return cls.files(path)


class RetiredSQLiteStore(SQLiteFile, RetiredRootFile):
    """An obsolete SQLite store and its -wal and -shm members."""


class RetiredPublishedSQLiteStore(PublishedSQLiteFile, RetiredSQLiteStore):
    @classmethod
    def retirement_paths(cls, path: Path) -> tuple[Path, ...]:
        return (*super().retirement_paths(path), *cls._source_paths(path))


class RetiredTodoAllocationSnapshot(RetiredSQLiteStore):
    filename = "todos.sqlite3.before-allocation-1792621659"


class RetiredTranscriptReplyIndex(RetiredSQLiteStore):
    filename = "transcript_reply_index.v4.sqlite3"


class RetiredWakeCandidateIndex(RetiredPublishedSQLiteStore):
    filename = "wake_candidate{revision}.sqlite3"


class RetiredBusPageIndex(RetiredPublishedSQLiteStore):
    filename = "bus_page_index{revision}.sqlite3"


class RetiredBusRouteCounts(RetiredPublishedSQLiteStore):
    filename = "bus_route_counts{revision}.sqlite3"


class RetiredBusCheckpoint(RetiredSQLiteStore):
    filename = "private_bus_checkpoint.sqlite3"


class RetiredWrappedOwnerReleases(RetiredSQLiteStore):
    root_directory = "owner_release_receipts.json"
    filename = "owner_releases.sqlite3"
