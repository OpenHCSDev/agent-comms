"""One-shot journal rewrite retaining exclusions, never creating send capabilities."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from abc import abstractmethod
from contextlib import closing
from dataclasses import dataclass, fields
from pathlib import Path
from typing import ClassVar, Literal

from agent_comms.compaction_journal import (
    CompactionJournal,
    CompactionOperation,
    CompactionPublication,
    EnrolledPrivateSession,
    JournalTable,
    PrivateRawInput,
    SelectedSummaryAttempt,
)
from agent_comms.compaction_states import OperationState, PublicationState, SummaryState
from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec
from agent_comms.store_files import _atomic_write_text, file_revision
from agent_comms.typed_table import TypedRow, TypedTable


class StoredJournalRow(DeclaredFamily, TypedRow):
    """Retired columns live only at this one-shot boundary."""

    target: ClassVar[type[TypedTable]]

    @abstractmethod
    def current(self) -> TypedTable: ...


@dataclass(frozen=True)
class StoredOperation(StoredJournalRow):
    target = CompactionOperation
    commit_id: str
    session_file: str
    intent_json: str
    status: str
    evidence_json: str | None

    def current(self) -> CompactionOperation:
        return self.target(
            self.commit_id,
            self.session_file,
            self.intent_json,
            OperationState.decode(self.status)(),
            self.evidence_json,
        )


@dataclass(frozen=True)
class StoredPublication(StoredJournalRow):
    target = CompactionPublication
    commit_id: str
    session_file: str
    metadata_json: str
    status: str

    def current(self) -> CompactionPublication:
        return self.target(
            self.commit_id,
            self.session_file,
            self.metadata_json,
            PublicationState.decode(self.status)(),
        )


@dataclass(frozen=True)
class StoredSummary(StoredJournalRow):
    target = SelectedSummaryAttempt
    operation_id: str
    session_file: str
    source_json: str
    status: str
    commit_id: str | None
    decline_reason: str | None

    def current(self) -> SelectedSummaryAttempt:
        state_type = SummaryState.decode(self.status)
        # The current state declaration owns which payload columns have meaning.
        payload = {item.name: getattr(self, item.name) for item in fields(state_type)}
        used = set(payload)
        for name in ("commit_id", "decline_reason"):
            if name not in used and getattr(self, name) is not None:
                raise ValueError(f"Unexpected retained {name} for {self.status}")
        state = FieldCodec.decode(SummaryState, {"kind": state_type.declared_name, **payload})
        return self.target(self.operation_id, self.session_file, self.source_json, state)


@dataclass(frozen=True)
class StoredRawInput(StoredJournalRow):
    target = PrivateRawInput
    input_id: str
    session_file: str
    status: Literal["unknown"]

    def current(self) -> PrivateRawInput:
        return self.target(self.input_id, self.session_file, self.status)


@dataclass(frozen=True)
class StoredEnrollment(StoredJournalRow):
    target = EnrolledPrivateSession
    session_file: str
    session_id: str
    device: int
    inode: int
    header_sha256: str
    owner_name: str
    owner_created_at: str
    owner_lookup: str
    owner_generation: int
    admission_epoch: int
    creator_pid: int

    def current(self) -> EnrolledPrivateSession:
        return self.target(
            **{
                item.name: getattr(self, item.name)
                for item in fields(self.target)
                if item.name != "admission_generation"
            },
            admission_generation=self.admission_epoch,
        )


@dataclass(frozen=True)
class JournalRewrite:
    source: str
    staged: str
    rows: dict[str, int]
    unresolved_operations: int
    unresolved_summaries: int


def stage(source: Path, destination: Path) -> JournalRewrite:
    """Write destination/compaction-commits.sqlite3 in a NEW staging directory.

    Source must be a quiet retained database. A complete SQLite backup preserves
    original evidence including WAL pages. Reopen equality covers every row;
    current journal methods retain barriers, but no terminal ACK, enrollment
    capability, provider attempt or reconciliation is issued. Session paths and
    inode witnesses are never rebound: copied inodes do not inherit native proof.
    Parent installs the current database without any prior SQLite sidecars.
    """
    source = source.resolve(strict=True)
    destination = destination.absolute()
    before = tuple(file_revision(Path(str(source) + suffix)) for suffix in ("", "-wal"))
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    original = destination / "original-compaction-commits.sqlite3"
    with (
        closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as source_db,
        closing(sqlite3.connect(original)) as snapshot,
    ):
        source_db.backup(snapshot)
        if snapshot.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError("Retained compaction journal failed integrity check")
    os.chmod(original, 0o600)
    declarations = StoredJournalRow.members_with(StoredJournalRow)
    if {row.target for row in declarations} != set(TypedTable.members_with(JournalTable)):
        raise ValueError("One-shot converters do not cover the current journal declarations")
    output = CompactionJournal(destination / "compaction-commits.sqlite3")
    counts = {}
    unresolved_operations = unresolved_summaries = 0
    with closing(sqlite3.connect(original.as_uri() + "?mode=ro", uri=True)) as old:
        names = {row[0] for row in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if names != {row.target.declared_name for row in declarations}:
            raise ValueError("Retained journal tables differ; refusing to drop evidence")
        with output._transaction() as current:
            current.execute("PRAGMA defer_foreign_keys=ON")
            for declaration in declarations:
                name = declaration.target.declared_name
                count = 0
                for row in declaration.iterate(
                    old.execute(f'SELECT * FROM "{name}" ORDER BY rowid')
                ):
                    row.current().insert(current)
                    count += 1
                counts[name] = count
            if current.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("Retained journal contains orphan publications")
        reopened = CompactionJournal(output.path)
        with reopened._transaction() as current:
            for declaration in declarations:
                name = declaration.target.declared_name
                wanted = [
                    row.current()
                    for row in declaration.iterate(
                        old.execute(f'SELECT * FROM "{name}" ORDER BY rowid')
                    )
                ]
                actual = declaration.target.read(
                    current.execute(f'SELECT * FROM "{name}" ORDER BY rowid')
                )
                if actual != wanted:
                    raise ValueError(f"Reopened journal evidence differs: {name}")
            unresolved_operations = sum(
                not row.state.terminal for row in CompactionOperation.select(current)
            )
            unresolved_summaries = sum(
                not row.state.terminal for row in SelectedSummaryAttempt.select(current)
            )
    if before != tuple(file_revision(Path(str(source) + suffix)) for suffix in ("", "-wal")):
        raise ValueError("Source journal changed during staging; do not install candidate")
    result = JournalRewrite(
        str(source), str(output.path), counts, unresolved_operations, unresolved_summaries
    )
    _atomic_write_text(
        destination / "journal-rewrite.json", json.dumps(FieldCodec.encode(result), indent=2)
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(json.dumps(FieldCodec.encode(stage(args.source, args.destination)), indent=2))
