"""Immutable original-source membership of one reserved triage input.

Full inputs derive membership from existing execution claims. This relation is
only for triage, before an execution exists. It contains no handling state.
"""

from dataclasses import dataclass
from typing import Annotated

from .coordination_errors import IdentityConflict
from .native_runtime_input import NativeRuntimeInput, NativeRuntimeTable
from .typed_table import (
    Column,
    GuardedInserts,
    ImmutableRows,
    WithoutRowid,
)


@dataclass(frozen=True)
class TriageNativeSources(
    GuardedInserts,
    ImmutableRows,
    NativeRuntimeTable,
    WithoutRowid,
    declared_name="native_runtime_triage_sources",
):
    insert_condition = """(NOT EXISTS (SELECT 1 FROM native_runtime_input n
        WHERE n.input_id=NEW.input_id AND n.stage='triage')) OR (EXISTS (
        SELECT 1 FROM json_each(NEW.assignment_ids) member LEFT JOIN wake_claims c
        ON c.assignment_id=member.value JOIN native_runtime_input n ON n.input_id=NEW.input_id
        WHERE c.assignment_id IS NULL OR c.recipient_lookup<>n.owner_lookup) OR EXISTS (
        SELECT 1 FROM native_runtime_triage_sources s, json_each(s.assignment_ids) previous
        JOIN json_each(NEW.assignment_ids) member ON member.value=previous.value))"""

    input_id: Annotated[
        str, Column(primary_key=True, references=(NativeRuntimeInput, "input_id"))
    ]
    assignment_ids: tuple[str, ...]

    checks = ("json_array_length(assignment_ids)>0",)

    def __post_init__(self):
        if not self.assignment_ids or len(set(self.assignment_ids)) != len(self.assignment_ids):
            raise IdentityConflict("Native input membership must name distinct original sources")
