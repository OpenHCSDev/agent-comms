"""Metadata rows own their SQL constraints and lifecycle relations."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field

from agent_comms.coordination_schema import CoordinatorTable
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
