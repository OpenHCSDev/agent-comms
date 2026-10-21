"""Carry old proof pairs once, using the exact installed build's package records.

Run through agent-comms install after owners stop. Delete this step once every
bus has run it; no old-proof reader remains in production.
"""

from __future__ import annotations

import json
from contextlib import closing, nullcontext
from dataclasses import dataclass
from pathlib import Path

from agent_comms.build_identity import BuildIdentity, RecordedBuildIdentity, UnrecordedBuildIdentity
from agent_comms.coordination_database import CoordinationStore
from agent_comms.field_codec import FieldCodec
from agent_comms.installer.carry import Carried, Carry
from agent_comms.installer.runtime_resolution import RuntimeResolution
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.locked_store import SQLiteFile
from agent_comms.todos import Todo, TodoStore
from agent_comms.typed_table import JsonStorage, TypedRow


@dataclass(frozen=True)
class StoredProofBuild(TypedRow):
    problem: str
    path: str
    core: str

    def carried(
        self, resolution: RuntimeResolution, installed: RecordedBuildIdentity
    ) -> BuildIdentity:
        return installed if self.core == resolution.core_commit else UnrecordedBuildIdentity()


class ProofBuildCommits(Carry):
    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        paths = tuple(path for path in SQLiteFile.files(TodoStore.path_in(root)) if path.exists())
        for path in paths:
            cls.require_confined(root, path)
        return paths

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        path = TodoStore.path_in(root)
        cls.require_confined(root, path)
        if not path.exists():
            return Carried(0, {"recorded": 0, "unrecorded": 0})
        runtime = LaunchRuntime.installed()
        runtime.require_available()
        resolution = RuntimeResolution.at(runtime.prefix)
        installed = RecordedBuildIdentity.from_resolution(resolution)
        recorded = unrecorded = 0
        table = Todo.declared_name
        identity = Todo._column_list(("id",))
        connection = (
            CoordinationStore.writer(path) if apply else CoordinationStore.read_snapshot(path)
        )
        with closing(connection) as db:
            with SQLiteFile.write_transaction(db) if apply else nullcontext(db):
                for declared in Todo._fields():
                    if not issubclass(declared.storage, JsonStorage):
                        continue
                    column = Todo._column_list((declared.name,))
                    proofs = StoredProofBuild.read(
                        db.execute(
                            f"SELECT todo.{identity} AS problem, entry.path AS path, "
                            f"json_extract(todo.{column}, entry.path || '.core') AS core "
                            f'FROM "{table}" AS todo, json_tree(todo.{column}) AS entry '
                            f"WHERE entry.key='kind' AND entry.value=? "
                            f"AND json_type(todo.{column}, entry.path || '.core')='text' "
                            f"AND json_type(todo.{column}, entry.path || '.toad')='text'",
                            (RecordedBuildIdentity.declared_name,),
                        )
                    )
                    for proof in proofs:
                        carried = proof.carried(resolution, installed)
                        matching = carried == installed
                        recorded += matching
                        unrecorded += not matching
                        if apply:
                            db.execute(
                                f'UPDATE "{table}" SET {column}=json_set({column}, ?, json(?)) WHERE {identity}=?',
                                (proof.path, json.dumps(FieldCodec.encode(carried)), proof.problem),
                            )
                if apply:
                    # Reject the whole transaction if any resulting canonical row cannot decode.
                    for _ in Todo.stream(db):
                        pass
        changed = recorded + unrecorded
        rewritten = (
            tuple(str(file.relative_to(root)) for file in SQLiteFile.files(path))
            if apply and changed
            else ()
        )
        return Carried(changed, {"recorded": recorded, "unrecorded": unrecorded}, rewritten)
