"""Immutable original-source membership of one reserved native input.

Execution claims own handling and response obligations. This relation owns which
of those originals were actually included in a single reserved native input,
including triage before an execution exists. It contains no handling state.
"""

from dataclasses import dataclass, field

from .coordination_errors import IdentityConflict, PublicationActivationBlocked
from .native_runtime_input import NativeRuntimeInput
from .private_runtime_schema import PrivateRuntimeSchema
from .typed_table import Column, SQLiteSchemaObject, TypedTable


@dataclass(frozen=True)
class SelectedNativeSources(TypedTable, PrivateRuntimeSchema):
    input_id: str = field(metadata={"sql": Column(primary_key=True, references=(NativeRuntimeInput, "input_id"))})
    assignment_ids: tuple[str, ...]

    without_rowid = True

    def __post_init__(self):
        if not self.assignment_ids or len(set(self.assignment_ids)) != len(self.assignment_ids):
            raise IdentityConflict("Native input membership must name distinct original sources")

    @classmethod
    def triggers(cls):
        return {
            f"{cls.declared_name}_{operation.lower()}_guard":
            f"CREATE TRIGGER {cls.declared_name}_{operation.lower()}_guard BEFORE {operation} "
            f"ON {cls.declared_name} BEGIN SELECT RAISE(ABORT,'native source membership is frozen'); END"
            for operation in ("UPDATE", "DELETE")
        }

    @classmethod
    def install(cls, store):
        with store.session.transaction() as db:
            present = SQLiteSchemaObject.read(db.execute(
                "SELECT name,sql FROM sqlite_master WHERE name=?", (cls.declared_name,)
            ))
            if not present:
                if NativeRuntimeInput.select(db):
                    raise PublicationActivationBlocked(
                        "Existing native inputs require explicit original-source membership attestation"
                    )
                for statement in cls.schema_objects().values():
                    db.execute(statement)
            cls.require_schema(db)

    @classmethod
    def require_schema(cls, db):
        expected = cls.schema_objects()
        rows = SQLiteSchemaObject.read(db.execute(
            "SELECT name,sql FROM sqlite_master WHERE name=? OR tbl_name=?",
            (cls.declared_name, cls.declared_name),
        ))
        actual = {row.name: row.sql for row in rows if row.sql is not None}
        if actual != expected:
            raise PublicationActivationBlocked("Native batch source schema is absent or differs")

    def require_members(self, assignments) -> None:
        if self.assignment_ids != tuple(row.assignment_id for row in assignments):
            raise IdentityConflict("Reserved native input changed its original batch membership")
