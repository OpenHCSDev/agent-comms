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
from agent_comms.native_pi import NativeContextJournal
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
class NativeProofDifference:
    missing_rows: int
    conflicting_keys: int

    def require_compatible(self) -> None:
        if self.conflicting_keys:
            raise RelationViolationError(
                f"Native proof history has {self.conflicting_keys} conflicting keys"
            )


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
    history_schema = "native_history"

    @contextmanager
    def connection(self, journal_root: Path, mode: Literal["ro", "rw"]) -> Iterator[sqlite3.Connection]:
        """SQLite URI modes are its external protocol, not domain dispatch."""
        self.require_sources(journal_root)
        self.require_targets()
        NativeSessionFileRole.require(self.input_proof.lstat())
        with closing(sqlite3.connect(self.input_proof.as_uri() + f"?mode={mode}", uri=True,
                                     timeout=30)) as db:
            db.execute("PRAGMA synchronous=EXTRA")
            NativeContextJournal.require_schema(db)
            db.execute(f"ATTACH DATABASE ? AS {self.history_schema}",
                       (self.source.as_uri() + "?mode=ro",))
            yield db

    def difference(self, db: sqlite3.Connection) -> NativeProofDifference:
        """Derive every key and compared column from the existing declaration."""
        table = NativeContextJournal
        same_key = " AND ".join(
            f'past."{item.wire_name}" = present."{item.wire_name}"'
            for item in table._fields() if item.column.primary_key
        )
        different = " OR ".join(
            f'past."{item.wire_name}" IS NOT present."{item.wire_name}"'
            for item in table._fields()
        )
        past = f"{self.history_schema}.{table.declared_name}"
        present = f"main.{table.declared_name}"
        missing = db.execute(
            f"SELECT COUNT(*) FROM {past} AS past WHERE NOT EXISTS "
            f"(SELECT 1 FROM {present} AS present WHERE {same_key})"
        ).fetchone()[0]
        conflicts = db.execute(
            f"SELECT COUNT(*) FROM {past} AS past JOIN {present} AS present "
            f"ON {same_key} WHERE {different}"
        ).fetchone()[0]
        return NativeProofDifference(missing, conflicts)

    def validate(self, db: sqlite3.Connection) -> None:
        """Validate immutable historical rows through the existing schema in memory.

        Independent journal lifetimes can reuse generations. The live append
        policy is not a rule for restoring older keyed evidence. No input runs.
        """
        self.difference(db).require_compatible()
        table = NativeContextJournal
        columns = table._column_list(tuple(table.column_projection()))
        rows = table.read(db.execute(
            f"SELECT {columns} FROM {self.history_schema}.{table.declared_name} UNION "
            f"SELECT {columns} FROM main.{table.declared_name}"
        ))
        with closing(sqlite3.connect(":memory:")) as validation:
            table.create_schema(validation)
            for name in table.trigger_bodies():
                validation.execute(f'DROP TRIGGER "{name}"')
            for row in rows:
                if row.session_id != self.session_id:
                    raise RelationViolationError("Native proof history belongs to another session")
                row.insert(validation)

    def merge(self, db: sqlite3.Connection) -> int:
        # Decode outside the writer lock; recheck every key inside the transaction.
        self.validate(db)
        table = NativeContextJournal
        with db:
            db.execute("BEGIN IMMEDIATE")
            difference = self.difference(db)
            difference.require_compatible()
            if not difference.missing_rows:
                return 0
            # Transactional DDL restores triggers on rollback too. Only the live
            # append policy is bypassed, never the declared row checks/keys.
            triggers = table.trigger_bodies()
            for name in triggers:
                db.execute(f'DROP TRIGGER "{name}"')
            columns = table._column_list(tuple(table.column_projection()))
            inserted = db.execute(
                f"INSERT INTO {table.declared_name} ({columns}) "
                f"SELECT {columns} FROM {self.history_schema}.{table.declared_name} "
                f"EXCEPT SELECT {columns} FROM main.{table.declared_name}"
            ).rowcount
            declarations = table.schema_objects()
            for name in triggers:
                db.execute(declarations[name])
        return inserted

    def inspect(self, journal_root: Path) -> ProofHistoryReport:
        with self.connection(journal_root, "ro") as db:
            difference = self.difference(db)
        return self.report(difference)

    def apply(self, journal_root: Path) -> ProofHistoryReport:
        with self.connection(journal_root, "rw") as db:
            difference = self.difference(db)
            inserted = self.merge(db)
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
