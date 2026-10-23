"""Retired store declarations. Delete with carry_retired_stores.py."""

from pathlib import Path

from agent_comms.locked_store import DurableFile, PatternFile, PublishedSQLiteFile, SQLiteFile


class RetiredRootFile(DurableFile, PatternFile):
    """Unwritten artifacts and their companions use byte copies."""

    @classmethod
    def primaries_in(cls, root: Path) -> frozenset[Path]:
        return super().matching_in(root)

    @classmethod
    def matching_in(cls, root: Path) -> frozenset[Path]:
        return frozenset(
            member
            for primary in cls.primaries_in(root)
            for member in cls.retirement_paths(primary)
            if member.is_file()
        )

    @classmethod
    def retirement_paths(cls, path: Path) -> tuple[Path, ...]:
        return cls.files(path)


class RetiredSQLiteStore(RetiredRootFile):
    """A retired database has no writer; preserve its physical companions as bytes."""

    @classmethod
    def retirement_paths(cls, path: Path) -> tuple[Path, ...]:
        return SQLiteFile.files(path)


class RetiredPublishedSQLiteStore(RetiredSQLiteStore):
    @classmethod
    def retirement_paths(cls, path: Path) -> tuple[Path, ...]:
        return (*super().retirement_paths(path), *PublishedSQLiteFile._source_paths(path))


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
