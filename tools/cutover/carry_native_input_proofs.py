"""Move native proof storage into its recorded participant's bus namespace.

Only this offline carry knows the retired sibling spelling. Registry absolute
paths are deliberately not followed: a practice copy must not mutate its source.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from agent_comms.coordination_database import CoordinationStore
from agent_comms.errors import RelationViolationError
from agent_comms.installer.carry import Carried, Carry
from agent_comms.locked_store import SQLiteFile
from agent_comms.native_pi import NativeContextJournal
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.native_session_files import NativeInputProof, NativeSessionFiles
from agent_comms.native_session_reopen import NativeSessionIdentity
from agent_comms.private_path import NativeSessionFileRole, fsync_directory
from cutover import carry_declared_schema


@dataclass(frozen=True, slots=True, kw_only=True)
class ProofRelocation(NativeSessionFiles):
    @property
    def source(self) -> Path:
        return self.session.with_name(self.session.name + ".input-proof")

    @property
    def source_files(self) -> tuple[Path, ...]:
        return tuple(path for path in SQLiteFile.files(self.source) if path.exists())

    def require_movable(self) -> None:
        NativeSessionFileRole.require(self.source.lstat())
        for source in self.source_files:
            Carry.require_confined(self.root, source)
            NativeSessionFileRole.require(source.lstat())
        for destination in SQLiteFile.files(self.input_proof):
            Carry.require_confined(self.root, destination)
            if destination.exists() or destination.is_symlink():
                raise RelationViolationError(f"Native proof destination already exists: {destination}")

    def relocate(self) -> tuple[Path, ...]:
        """Move companions before the primary; undo a reported rename failure.

        Process death between renames leaves preserved files and an explicit
        collision on restart, never an inferred successful move or lost WAL.
        """
        present = self.source_files
        if self.source not in present:
            raise FileNotFoundError(self.source)
        self.prepare_private_directory(self.input_proof.parent)
        moved: list[Path] = []
        try:
            for source, destination in reversed(tuple(zip(
                SQLiteFile.files(self.source), SQLiteFile.files(self.input_proof), strict=True,
            ))):
                if source in present:
                    source.rename(destination)
                    moved.append(source)
                    fsync_directory(destination.parent)
                    fsync_directory(source.parent)
        except BaseException as error:
            failures = [error]
            for source, destination in zip(
                SQLiteFile.files(self.source), SQLiteFile.files(self.input_proof), strict=True,
            ):
                if source in moved:
                    try:
                        destination.rename(source)
                        fsync_directory(source.parent)
                        fsync_directory(destination.parent)
                    except OSError as rollback:
                        failures.append(rollback)
            if len(failures) > 1:
                raise BaseExceptionGroup("Native proof move and rollback failed", failures)
            raise
        return (*moved, *(path for path in SQLiteFile.files(self.input_proof) if path.exists()))


class NativeInputProofs(Carry):
    @classmethod
    def relocations(cls, root: Path) -> tuple[ProofRelocation, ...]:
        found = set(root.rglob("*.input-proof*"))
        primaries = sorted(path for path in found if path.name.endswith(".input-proof"))
        if not primaries:
            if found:
                raise RelationViolationError(f"Native proof companions have no primary: {sorted(found)}")
            return ()
        plans: list[ProofRelocation] = []
        coordinator = CoordinationStore.path_in(root)
        for path in SQLiteFile.files(coordinator):
            cls.require_confined(root, path)
        with closing(CoordinationStore.read_snapshot(coordinator)) as db:
            for primary in primaries:
                cls.require_confined(root, primary)
                source = primary.with_name(primary.name.removesuffix(".input-proof"))
                cls.require_confined(root, source)
                NativeSessionFileRole.require(source.lstat())
                identity = NativeSessionIdentity.locate(str(source))
                inputs = NativeRuntimeInput.select(
                    db, where="session_id=?", parameters=(identity.session_id,),
                )
                owners = {item.owner_identity.lookup: item.owner_identity for item in inputs}
                if len(owners) != 1:
                    raise RelationViolationError(
                        f"Native proof session has no unique recorded owner: {source} ({len(owners)} owners)"
                    )
                plan = ProofRelocation.for_identity(identity, root=root, owner=next(iter(owners.values())))
                plan.require_movable()
                plans.append(plan)
        declared = {path for plan in plans for path in plan.source_files}
        if stray := found - declared:
            raise RelationViolationError(f"Native proof has unrecognized companions: {sorted(stray)}")
        targets = [plan.input_proof for plan in plans]
        if len(set(targets)) != len(targets):
            raise RelationViolationError("Multiple native proofs name the same participant/session")
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
        plans = cls.relocations(root)  # Validate every source before moving any of them.
        if not apply:
            return Carried(len(plans))
        written = {path for plan in plans for path in plan.relocate()}
        converted = 0
        for path in sorted(NativeInputProof.matching_in(root)):
            cls.require_confined(root, path)
            NativeSessionFileRole.require(path.lstat())
            # SQLite recovers its moved companions before catalog inspection.
            with closing(CoordinationStore.recovering_snapshot(path)) as db:
                differs = NativeContextJournal.schema_differs(db)
            if differs:
                carry_declared_schema.carry(path)
                converted += 1
                written.update(SQLiteFile.files(path))
        return Carried(len(plans) + converted, {"proofs": len(plans), "schemas": converted},
                       tuple(str(path.relative_to(root)) for path in sorted(written)))
