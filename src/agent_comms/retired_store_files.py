"""File declarations required to snapshot and restore superseded store artifacts.

The cutover tool deletes these resources. Their declarations remain available to
cold backup recovery while retained snapshots still name them.
"""

from pathlib import Path

from agent_comms.locked_store import DurableFile, PatternFile, PublishedSQLiteFile, RootFile, SQLiteFile


class RetiredRootFile(DurableFile, PatternFile):
    """An obsolete resource remains declared until its carry and rollback finish."""

    @classmethod
    def retirement_paths(cls, path: Path) -> tuple[Path, ...]:
        return cls.files(path)


class RetiredSQLiteStore(SQLiteFile, RetiredRootFile):
    """Declared SQLite snapshot behavior remains available throughout rollback."""


class RetiredPublishedSQLiteStore(PublishedSQLiteFile, RetiredSQLiteStore):
    @classmethod
    def copy_group(cls, source: Path, target: Path) -> frozenset[Path]:
        copied = set(super().copy_group(source, target))
        for original, destination in zip(cls._source_paths(source), cls._source_paths(target), strict=True):
            if original.exists():
                RootFile.copy_file(original, destination)
                copied.add(original)
        return frozenset(copied)

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
