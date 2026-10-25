"""Immutable original-source membership of one reserved triage input.

Full inputs derive membership from existing execution claims. This relation is
only for triage, before an execution exists: one row per member, keyed and
ordered like ``ExecutionAssignmentLink``. It contains no handling state.
"""

from dataclasses import dataclass
from typing import Annotated

from .field_codec import AssignmentId, InputId
from .native_runtime_input import NativeRuntimeInput, NativeRuntimeTable
from .typed_table import (
    Column,
    GuardedInserts,
    ImmutableRows,
    TableReference,
    WithoutRowid,
)


@dataclass(frozen=True)
class TriageNativeSource(
    GuardedInserts,
    ImmutableRows,
    NativeRuntimeTable,
    WithoutRowid,
    declared_name="native_runtime_triage_members",
):
    insert_condition = """(NOT EXISTS (SELECT 1 FROM native_runtime_input n
        WHERE n.input_id=NEW.input_id AND n.stage='triage'))
        OR NEW.ordinal != (SELECT count(*) FROM native_runtime_triage_members
        WHERE input_id=NEW.input_id)
        OR NOT EXISTS (SELECT 1 FROM wake_claims c JOIN native_runtime_input n
        ON n.input_id=NEW.input_id
        WHERE c.assignment_id=NEW.assignment_id AND c.recipient_lookup=n.owner_lookup)"""

    input_id: Annotated[
        InputId,
        Column(primary_key=True, references=TableReference(NativeRuntimeInput, ("input_id",)))
    ]
    ordinal: Annotated[int, Column(primary_key=True, check="ordinal>=0")]
    # An original source joins at most one triage input; the uniqueness also indexes the
    # notification reads that select membership by assignment.
    assignment_id: Annotated[AssignmentId, Column(unique=True)]
