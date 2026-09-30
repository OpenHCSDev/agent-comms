"""Metadata rows own their SQL constraints and lifecycle relations."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field

from agent_comms.coordination_errors import SchemaVersionError
from agent_comms.coordination_schema import (
    COORDINATION_SCHEMA_VERSION,
    COORDINATION_SNAPSHOT_VERSION,
    CoordinatorTable,
)
from agent_comms.typed_table import (
    Column,
    TypedTable,
)


@dataclass(frozen=True, kw_only=True)
class SchemaMeta(CoordinatorTable, TypedTable):
    singleton: int = dataclass_field(
        metadata={"sql": Column(primary_key=True, check="singleton = 1")}
    )
    schema_version: int
    snapshot_version: int

    @classmethod
    def current(cls):
        return cls(
            singleton=1,
            schema_version=COORDINATION_SCHEMA_VERSION,
            snapshot_version=COORDINATION_SNAPSHOT_VERSION,
        )

    @classmethod
    def require_current(cls, db, version: int) -> None:
        expected = cls.current()
        if version != expected.schema_version:
            raise SchemaVersionError(
                f"coordination schema {version} is unsupported; "
                f"expected {expected.schema_version}"
            )
        if cls.one(db, singleton=expected.singleton) != expected:
            raise SchemaVersionError("coordination schema metadata is inconsistent")

    @classmethod
    def triggers(cls):
        return {
            "schema_meta_update_frozen": (
                """CREATE TRIGGER schema_meta_update_frozen BEFORE UPDATE ON schema_meta BEGIN
    SELECT RAISE(ABORT, 'schema metadata is immutable');
END"""
            ),
            "schema_meta_delete_frozen": (
                """CREATE TRIGGER schema_meta_delete_frozen BEFORE DELETE ON schema_meta BEGIN
    SELECT RAISE(ABORT, 'schema metadata cannot be deleted');
END"""
            ),
        }
