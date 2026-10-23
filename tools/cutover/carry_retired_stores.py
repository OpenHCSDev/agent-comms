"""Retire superseded stores after their writers and consumers have been removed."""

from pathlib import Path
from typing import ClassVar

from agent_comms.installer.carry import Carried, Carry
from agent_comms.locked_store import DurableFile, PatternFile, PublishedSQLiteFile, RootFile, SQLiteFile
from agent_comms.typed_table import HeldBusFile


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


class RetiredProbeWriterLock(RetiredRootFile):
    filename = "sessions/{session}.pr48-writer.lock"


class RetiredStores(Carry):
    opened_files: ClassVar[tuple[type[RetiredRootFile], ...]] = (RetiredRootFile,)

    def carry(self, *, apply: bool) -> Carried:
        owners = {path: owner for family in self.opened_files
                  for path, owner in family.existing_in(self.root).items()}
        rewritten: list[Path] = []
        for path, owner in owners.items():
            self.require_confined(self.root, path)
            if apply:
                HeldBusFile(owner, path).require()
                for member in owner.retirement_paths(path):
                    if member.exists():
                        member.unlink()
                        rewritten.append(member.relative_to(self.root))
        return Carried(len(owners), rewritten=tuple(rewritten))
