"""Immutable original-source membership of one reserved triage input.

Full inputs derive membership from existing execution claims. This relation is
only for triage, before an execution exists. It contains no handling state.
"""

from dataclasses import dataclass, field

from .coordination_errors import IdentityConflict
from .native_runtime_input import NativeRuntimeInput, NativeRuntimeTable
from .typed_table import Column, TypedTable


@dataclass(frozen=True)
class TriageNativeSources(NativeRuntimeTable, TypedTable, declared_name="native_runtime_triage_sources"):
    input_id: str = field(metadata={"sql": Column(primary_key=True, references=(NativeRuntimeInput, "input_id"))})
    assignment_ids: tuple[str, ...]

    without_rowid = True
    checks = ("json_array_length(assignment_ids)>0",)

    def __post_init__(self):
        if not self.assignment_ids or len(set(self.assignment_ids)) != len(self.assignment_ids):
            raise IdentityConflict("Native input membership must name distinct original sources")

    @classmethod
    def triggers(cls):
        from .native_input_record import TriageNativeExecution

        guards = {
            f"{cls.declared_name}_{operation.lower()}_guard":
            f"CREATE TRIGGER {cls.declared_name}_{operation.lower()}_guard BEFORE {operation} "
            f"ON {cls.declared_name} BEGIN SELECT RAISE(ABORT,'native source membership is frozen'); END"
            for operation in ("UPDATE", "DELETE")
        }
        guards[f"{cls.declared_name}_stage_guard"] = (
            f"CREATE TRIGGER {cls.declared_name}_stage_guard BEFORE INSERT ON {cls.declared_name} "
            "WHEN NOT EXISTS (SELECT 1 FROM native_runtime_input n "
            f"WHERE n.input_id=NEW.input_id AND n.stage='{TriageNativeExecution.declared_name}') "
            "BEGIN SELECT RAISE(ABORT,'only triage owns separate source membership'); END"
        )
        guards[f"{cls.declared_name}_membership_guard"] = (
            f"CREATE TRIGGER {cls.declared_name}_membership_guard BEFORE INSERT ON {cls.declared_name} "
            "WHEN EXISTS (SELECT 1 FROM json_each(NEW.assignment_ids) member "
            "LEFT JOIN wake_claims c ON c.assignment_id=member.value "
            "JOIN native_runtime_input n ON n.input_id=NEW.input_id "
            "WHERE c.assignment_id IS NULL OR c.recipient_lookup<>n.owner_lookup) "
            f"OR EXISTS (SELECT 1 FROM {cls.declared_name} s, json_each(s.assignment_ids) previous "
            "JOIN json_each(NEW.assignment_ids) member ON member.value=previous.value) "
            "BEGIN SELECT RAISE(ABORT,'triage source is absent, belongs to another owner or was reserved'); END"
        )
        return guards

