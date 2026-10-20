"""Carry referenced JSON proofs into the indexed journal during a stopped upgrade.

The coordinator's recorded generation supplies the timestamp absent from schema1.
Original journals remain in the upgrade's store backup; this step grants no input
acceptance and never changes the saved session or coordinator records.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from agent_comms.coordination_database import CoordinationStore
from agent_comms.field_codec import FieldCodec
from agent_comms.native_entries import NativeInputEvidenceRead
from agent_comms.native_input_record import NativeInputCommit, NativeInputLocation
from agent_comms.native_pi import NativeContextJournal, NativeContextRecord
from agent_comms.native_session_files import NativeSessionFiles
from agent_comms.private_path import FileRevision, NativeSessionFileRole, fsync_directory
from agent_comms.store_files import StoreOwner
from agent_comms.typed_table import TypedRow

from .carry import Carried, Carry


@dataclass(frozen=True)
class JsonProof(NativeInputCommit):
    """The frozen schema1 boundary, before native context timestamps existed."""

    schema: Literal[1]
    type: Literal["context_committed"]
    request_generation: int = field(metadata={"wire_name": "requestGeneration"})
    llm_context_digest: str = field(metadata={"wire_name": "llmContextDigest"})

    def matches(self, recorded: RecordedProof) -> bool:
        return (
            self.input_commit == recorded.input_commit
            and self.request_generation == recorded.request_generation
            and self.llm_context_digest == recorded.llm_context_digest
        )


@dataclass(frozen=True)
class RecordedProof(NativeContextRecord, NativeInputLocation, TypedRow):
    """The coordinator's recorded context and byte placement, projected for carry."""

    def indexed(self) -> NativeContextJournal:
        return NativeContextJournal(
            self.input_id,
            self.session_id,
            self.session_entry_id,
            self.request_generation,
            self.llm_context_digest,
            self.committed_at_ms,
            schema=2,
            type="context_committed",
        )


class NativeProofJournals(Carry):
    @classmethod
    def sessions(cls, root: Path) -> tuple[tuple[Path, NativeSessionFiles], ...]:
        store = root / "coordination.sqlite3"
        if not store.exists():
            return ()
        with closing(CoordinationStore.read_snapshot(store)) as db:
            sources = []
            for file, owner in db.execute(
                "SELECT DISTINCT session_file,owner_lookup FROM native_runtime_input "
                "WHERE session_file IS NOT NULL ORDER BY session_file"
            ):
                original = Path(file)
                # A rehearsal copies the bus without rewriting SQL identities.
                # Its bus-owned session belongs to the copied directory, not the live one.
                local = original
                if (
                    original.parent.name == owner
                    and original.parent.parent.name == "native-sessions"
                ):
                    local = root / original.parent.parent.name / owner / original.name
                sources.append((original, NativeSessionFiles(local)))
            return tuple(sources)

    @classmethod
    def pending(cls, root: Path) -> tuple[tuple[Path, NativeSessionFiles], ...]:
        pending = []
        for original, files in cls.sessions(root):
            if not files.input_proof.exists():
                continue
            NativeSessionFileRole.require(files.input_proof.lstat())
            if not CoordinationStore.is_database(files.input_proof):
                # StoreBackup owns only this bus. Never mutate an unbacked external file.
                files.input_proof.relative_to(root)
                pending.append((original, files))
        return tuple(pending)

    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        return tuple(files.input_proof for _original, files in cls.pending(root))

    @classmethod
    def records(
        cls, db: sqlite3.Connection, original: Path, files: NativeSessionFiles
    ) -> tuple[NativeContextJournal, ...]:
        recorded = tuple(
            RecordedProof.read(
                db.execute(
                    "SELECT input_id,session_id,session_entry_id,request_generation,"
                    "llm_context_digest,committed_at_ms,session_entry_start "
                    "FROM native_runtime_input "
                    "WHERE session_file=? AND session_entry_id IS NOT NULL",
                    (str(original),),
                )
            )
        )
        proofs = []
        for line in files.input_proof.read_bytes().splitlines(keepends=True):
            if not line.endswith(b"\n"):
                raise ValueError(f"Incomplete original proof: {files.input_proof}")
            proofs.append(FieldCodec.decode(JsonProof, FieldCodec.parse(line)))
        indexed = []
        with NativeInputEvidenceRead.open(files.session) as evidence:
            for record in recorded:
                if sum(proof.matches(record) for proof in proofs) != 1:
                    raise ValueError(
                        f"Original proof does not corroborate recorded input: {files.input_proof}"
                    )
                row = record.indexed()
                row.corroborate(
                    files.session,
                    evidence.header(),
                    evidence.read_input(record),
                    session_entry_start=record.session_entry_start,
                )
                indexed.append(row)
            evidence.source.verify()
        return tuple(sorted(indexed, key=lambda row: row.request_generation))

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        pending = cls.pending(root)
        if not pending:
            return Carried(0)
        rewritten = []
        with closing(CoordinationStore.read_snapshot(root / "coordination.sqlite3")) as source:
            for original, files in pending:
                before = FileRevision.from_stat(files.input_proof.lstat())
                rows = cls.records(source, original, files)
                # SQLite enforces the same append, lineage and duplicate constraints
                # during inspection as during publication; a dry run writes no files.
                with closing(sqlite3.connect(":memory:")) as checked:
                    NativeContextJournal.create(checked)
                    with checked:
                        for row in rows:
                            row.insert(checked)
                    if apply:
                        descriptor, staged = StoreOwner.stage_file(files.input_proof)
                        os.close(descriptor)
                        try:
                            with closing(sqlite3.connect(staged)) as target:
                                checked.backup(target)
                                if target.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                                    raise ValueError(
                                        "Carried native proof failed integrity verification"
                                    )
                            with staged.open("rb") as complete:
                                os.fsync(complete.fileno())
                            if FileRevision.from_stat(files.input_proof.lstat()) != before:
                                raise ValueError("Original proof changed during stopped carry")
                            os.replace(staged, files.input_proof)
                            fsync_directory(files.input_proof.parent)
                        finally:
                            staged.unlink(missing_ok=True)
                        rewritten.append(str(files.input_proof.relative_to(root)))
        return Carried(len(pending), rewritten=tuple(rewritten))


if __name__ == "__main__":
    NativeProofJournals.main()
