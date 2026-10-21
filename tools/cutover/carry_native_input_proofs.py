"""Carry bus-contained resources; reconcile explicitly selected external history.

The source root never supplies coordinator or target authority. Original external
journals are read-only; only the bus root owns every target path.
"""
from __future__ import annotations

import argparse
import json
from abc import abstractmethod
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Annotated, Iterator, Literal

from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_tables.participants import OwnerGenerations
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec, PathText
from agent_comms.installer.carry import Carried, Carry
from agent_comms.locked_store import SQLiteFile
from agent_comms.native_pi import NativeContextJournal, NativeProofDifference
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.native_session_files import NativeInputProof, NativeSessionFiles
from agent_comms.native_session_reopen import NativeSessionIdentity
from agent_comms.private_path import NativeSessionFileRole, fsync_directory
from cutover import carry_declared_schema


@dataclass(frozen=True, slots=True, kw_only=True)
class LegacyNativeProof(NativeSessionFiles):
    """The retired spelling and its original coordinator-owned participant."""

    owner: OwnerGenerations

    @property
    def source(self) -> Path:
        return self.session.with_name(self.session.name + ".input-proof")

    @property
    def source_files(self) -> tuple[Path, ...]:
        return tuple(path for path in SQLiteFile.files(self.source) if path.exists())

    def require_sources(self, journal_root: Path) -> None:
        for source in (*self.source_files, self.session):
            Carry.require_confined(journal_root, source)
            NativeSessionFileRole.require(source.lstat())

    def require_targets(self) -> None:
        for target in SQLiteFile.files(self.input_proof):
            Carry.require_confined(self.root, target)


class ProofRelocation(LegacyNativeProof):
    def require_movable(self) -> None:
        self.require_sources(self.root)
        self.require_targets()
        for destination in SQLiteFile.files(self.input_proof):
            if destination.exists() or destination.is_symlink():
                raise RelationViolationError(f"Native proof destination already exists: {destination}")

    def relocate(self) -> tuple[Path, ...]:
        """Move bus-contained companions before the primary; roll back failures."""
        present = self.source_files
        if self.source not in present:
            raise FileNotFoundError(self.source)
        self.prepare_private_directory(self.input_proof.parent)
        moves = tuple(zip(SQLiteFile.files(self.source), SQLiteFile.files(self.input_proof), strict=True))
        moved: list[Path] = []
        with ExitStack() as rollback:
            for source, destination in reversed(moves):
                if source in present:
                    source.rename(destination)
                    rollback.callback(fsync_directory, destination.parent)
                    rollback.callback(fsync_directory, source.parent)
                    rollback.callback(destination.rename, source)
                    moved.append(source)
                    fsync_directory(destination.parent)
                    fsync_directory(source.parent)
            rollback.pop_all()
        return (*moved, *(path for path in SQLiteFile.files(self.input_proof) if path.exists()))


@dataclass(frozen=True)
class ProofHistoryReport:
    source: Annotated[Path, PathText]
    target: Annotated[Path, PathText]
    owner: OwnerGenerations
    difference: NativeProofDifference
    inserted_rows: int = 0
    message: str = "existing target"


class ProofHistory(LegacyNativeProof):
    def report(self, difference: NativeProofDifference, *, inserted: int = 0,
               message: str = "existing target") -> ProofHistoryReport:
        return ProofHistoryReport(self.source, self.input_proof, self.owner,
                                  difference, inserted, message)

    @abstractmethod
    def inspect(self, journal_root: Path) -> ProofHistoryReport: ...

    @abstractmethod
    def apply(self, journal_root: Path) -> ProofHistoryReport: ...


class MissingProofTarget(ProofHistory):
    def inspect(self, journal_root: Path) -> ProofHistoryReport:
        self.require_sources(journal_root)
        self.require_targets()
        with closing(CoordinationStore.read_snapshot(self.source)) as db:
            rows = NativeContextJournal.count(db)
        return self.report(NativeProofDifference(rows, 0), message="skipped: target absent; original preserved")

    def apply(self, journal_root: Path) -> ProofHistoryReport:
        return self.inspect(journal_root)


class ExistingProofTarget(ProofHistory):
    @contextmanager
    def connection(self, journal_root: Path, mode: Literal["ro", "rw"]) -> Iterator[sqlite3.Connection]:
        """SQLite URI modes are the external read/write capability, not a kind switch."""
        self.require_sources(journal_root)
        self.require_targets()
        NativeSessionFileRole.require(self.input_proof.lstat())
        with closing(sqlite3.connect(self.input_proof.as_uri() + f"?mode={mode}", uri=True,
                                     timeout=30)) as db:
            db.execute("PRAGMA synchronous=EXTRA")
            NativeContextJournal.require_schema(db)
            db.execute(f"ATTACH DATABASE ? AS {NativeContextJournal.history_schema}",
                       (self.source.as_uri() + "?mode=ro",))
            yield db

    def inspect(self, journal_root: Path) -> ProofHistoryReport:
        with self.connection(journal_root, "ro") as db:
            difference = NativeContextJournal.history_difference(db)
        return self.report(difference)

    def apply(self, journal_root: Path) -> ProofHistoryReport:
        with self.connection(journal_root, "rw") as db:
            difference = NativeContextJournal.history_difference(db)
            inserted = NativeContextJournal.merge_history(db, session_id=self.session_id)
        return self.report(difference, inserted=inserted)


class NativeInputProofs(Carry):
    @classmethod
    def sources(cls, root: Path, journal_root: Path) -> tuple[LegacyNativeProof, ...]:
        found = set(journal_root.rglob("*.input-proof*"))
        primaries = sorted(path for path in found if path.name.endswith(".input-proof"))
        coordinator = CoordinationStore.path_in(root)
        for path in SQLiteFile.files(coordinator):
            cls.require_confined(root, path)
        plans: list[LegacyNativeProof] = []
        if primaries:
            with closing(CoordinationStore.read_snapshot(coordinator)) as db:
                for primary in primaries:
                    cls.require_confined(journal_root, primary)
                    source = primary.with_name(primary.name.removesuffix(".input-proof"))
                    cls.require_confined(journal_root, source)
                    NativeSessionFileRole.require(source.lstat())
                    identity = NativeSessionIdentity.locate(str(source))
                    inputs = NativeRuntimeInput.select(db, session_id=identity.session_id)
                    owners = {item.owner_identity.lookup: item.owner_identity for item in inputs}
                    if len(owners) != 1:
                        raise RelationViolationError(
                            f"Native proof session has no unique recorded owner: {source} ({len(owners)} owners)"
                        )
                    plan = LegacyNativeProof.for_identity(identity, root=root, owner=next(iter(owners.values())))
                    plan.require_sources(journal_root)
                    plan.require_targets()
                    plans.append(plan)
        declared = {path for plan in plans for path in plan.source_files}
        if stray := found - declared:
            raise RelationViolationError(f"Native proof has unrecognized companions: {sorted(stray)}")
        targets = [plan.input_proof for plan in plans]
        if len(set(targets)) != len(targets):
            raise RelationViolationError("Multiple native proofs name the same participant/session")
        return tuple(plans)

    @classmethod
    def relocations(cls, root: Path) -> tuple[ProofRelocation, ...]:
        root = root.resolve(strict=True)
        plans = tuple(ProofRelocation.for_identity(source, root=root, owner=source.owner)
                      for source in cls.sources(root, root))
        for plan in plans:
            plan.require_movable()
        return plans

    @classmethod
    def histories(cls, root: Path, journal_root: Path) -> tuple[ProofHistory, ...]:
        root = root.resolve(strict=True)
        journal_root = journal_root.resolve(strict=True)
        plans: list[ProofHistory] = []
        for source in cls.sources(root, journal_root):
            # Decode filesystem presence once; consumers use the resulting capability.
            kind = ExistingProofTarget if source.input_proof.exists() else MissingProofTarget
            plans.append(kind.for_identity(source, root=root, owner=source.owner))
        return tuple(plans)

    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        root = root.resolve(strict=True)
        return tuple(path for plan in cls.relocations(root) for path in plan.source_files) + tuple(
            path for primary in NativeInputProof.matching_in(root)
            for path in SQLiteFile.files(primary) if path.exists()
        )

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        root = root.resolve(strict=True)
        plans = cls.relocations(root)
        if not apply:
            return Carried(len(plans))
        written = {path for plan in plans for path in plan.relocate()}
        converted = 0
        for path in sorted(NativeInputProof.matching_in(root)):
            cls.require_confined(root, path)
            NativeSessionFileRole.require(path.lstat())
            with closing(CoordinationStore.recovering_snapshot(path)) as db:
                differs = NativeContextJournal.schema_differs(db)
            if differs:
                carry_declared_schema.carry(path)
                converted += 1
                written.update(SQLiteFile.files(path))
        return Carried(len(plans) + converted, {"proofs": len(plans), "schemas": converted},
                       tuple(str(path.relative_to(root)) for path in sorted(written)))


class PreviewHistory:
    @classmethod
    def run(cls, root: Path, journal_root: Path) -> tuple[ProofHistory, ...]:
        plans = NativeInputProofs.histories(root, journal_root)
        for plan in plans:
            print(json.dumps(FieldCodec.encode(plan.inspect(journal_root))), flush=True)
        return plans


class ApplyHistory(PreviewHistory):
    @classmethod
    def run(cls, root: Path, journal_root: Path) -> tuple[ProofHistory, ...]:
        plans = super().run(root, journal_root)  # Print every comparison before any write.
        for plan in plans:
            print(json.dumps(FieldCodec.encode(plan.apply(journal_root))), flush=True)
        return plans


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bus-root", type=Path, required=True)
    parser.add_argument("--journal-root", type=Path, required=True)
    parser.add_argument("--apply", dest="operation", action="store_const", const=ApplyHistory,
                        default=PreviewHistory)
    args = parser.parse_args()
    args.operation.run(args.bus_root, args.journal_root)


if __name__ == "__main__":
    main()
