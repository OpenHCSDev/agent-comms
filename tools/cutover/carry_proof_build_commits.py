"""Clear incomplete old proof pairs once; never infer their Textual revision.

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
from agent_comms.locked_store import SQLiteFile
from agent_comms.todos import CheckedDelivery, OrderDelivery, Todo, TodoStore
from agent_comms.typed_table import JsonStorage, TypedRow


@dataclass(frozen=True)
class StoredProofBuild(TypedRow):
    problem: str
    path: str
    incomplete: bool


class ProofBuildCommits(Carry):
    def backup_files(self) -> tuple[Path, ...]:
        root = self.root
        paths = tuple(path for path in SQLiteFile.files(TodoStore.path_in(root)) if path.exists())
        for path in paths:
            self.require_confined(root, path)
        return paths

    def carry(self) -> Carried:
        root, apply = self.root, self.apply
        path = TodoStore.path_in(root)
        self.require_confined(root, path)
        if not path.exists():
            return Carried(0, {"cleared": 0, "unchanged": 0})
        cleared = unchanged = 0
        table = Todo.declared_name
        identity = Todo._column_list(("id",))
        kinds = OrderDelivery.members_with(CheckedDelivery)
        placeholders = ",".join("?" for _ in kinds)
        unrecorded = json.dumps(FieldCodec.encode(UnrecordedBuildIdentity()))
        connection = (
            CoordinationStore.writer(path) if apply else CoordinationStore.read_snapshot(path)
        )
        with closing(connection) as db:
            with SQLiteFile.write_transaction(db) if apply else nullcontext(db):
                for declared in Todo._fields():
                    if not issubclass(declared.storage, JsonStorage):
                        continue
                    column = Todo._column_list((declared.name,))
                    proofs = tuple(
                        StoredProofBuild.read(
                            db.execute(
                                f"SELECT todo.{identity} AS problem, entry.fullkey AS path, "
                                f"(json_extract(entry.value, '$.kind') IS ? "
                                f"AND json_type(entry.value, '$.core') IS 'text' "
                                f"AND json_type(entry.value, '$.toad') IS 'text') AS incomplete "
                                f'FROM "{table}" AS todo, json_tree(todo.{column}) AS entry '
                                f"WHERE entry.key='proof_build' "
                                f"AND json_extract(todo.{column}, entry.path || '.kind') IN ({placeholders})",
                                (
                                    FieldCodec.encode(RecordedBuildIdentity, type[BuildIdentity]),
                                    *(
                                        FieldCodec.encode(member, type[OrderDelivery])
                                        for member in kinds
                                    ),
                                ),
                            )
                        )
                    )
                    for proof in proofs:
                        cleared += proof.incomplete
                        unchanged += not proof.incomplete
                        if apply and proof.incomplete:
                            db.execute(
                                f'UPDATE "{table}" SET {column}=json_set({column}, ?, json(?)) WHERE {identity}=?',
                                (proof.path, unrecorded, proof.problem),
                            )
                if apply:
                    # Reject the whole transaction if any resulting canonical row cannot decode.
                    for _ in Todo.stream(db):
                        pass
        rewritten = (
            tuple(str(file.relative_to(root)) for file in SQLiteFile.files(path))
            if apply and cleared
            else ()
        )
        return Carried(cleared, {"cleared": cleared, "unchanged": unchanged}, rewritten)
