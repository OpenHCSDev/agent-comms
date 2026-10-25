"""Recovery rows own their SQL constraints and lifecycle relations."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Annotated

from agent_comms import field_codec
from agent_comms.coordination_contracts import (
    MAX_SANITIZED_DETAIL_CHARS,
    validate_execution_id,
)
from agent_comms.coordination_schema import CoordinatorRow
from agent_comms.coordination_tables.attempts import AttemptRecord
from agent_comms.coordination_tables.executions import ExecutionReference
from agent_comms.reason_codes import GivenReasonCode, RecoveryDetail
from agent_comms.recovery_states import RecoveryCondition
from agent_comms.typed_table import (
    Column,
    ForeignKey,
    ImmutableRows,
    Index,
    RowId,
    TextStorage,
    UnallocatedRowId,
)


@dataclass(frozen=True, slots=True)
class RecoveryAudit(ImmutableRows, ExecutionReference, CoordinatorRow):
    # Recovery condition is a stored fact; row identity is the table family tag.

    execution_id: Annotated[str, field_codec.SnapshotExclude()]
    kind: Annotated[type[RecoveryCondition], Column(check="kind IN ({recovery_names})")]
    reason_code: Annotated[
        GivenReasonCode, Column(storage=TextStorage, check="length(reason_code) BETWEEN 1 AND 64")
    ]
    sanitized_detail: Annotated[
        RecoveryDetail,
        Column(
            storage=TextStorage,
            nullable=True,
            check=(
                "sanitized_detail IS NULL OR length(sanitized_detail)<="
                f"{MAX_SANITIZED_DETAIL_CHARS}"
            )
        ),
    ]
    attempt: Annotated[int, Column(check="attempt>0")]
    elapsed_ms: Annotated[int, Column(check="elapsed_ms>=0")]
    observed_at_ms: Annotated[int, Column(check="observed_at_ms>=0")]
    audit_id: Annotated[
        RowId, Column(primary_key=True, auto_increment=True), field_codec.SnapshotExclude()
    ] = dataclass_field(default_factory=UnallocatedRowId)
    indexes = (Index(("execution_id", "audit_id")),)

    @classmethod
    def references(cls) -> tuple[ForeignKey, ...]:
        return super().references() + (
            ForeignKey(
                columns=("execution_id", "attempt"), target=AttemptRecord, target_columns=("execution_id", "attempt_ordinal")
            ),
        )

    def __post_init__(self) -> None:
        validate_execution_id(self.execution_id)
        if self.attempt <= 0 or self.elapsed_ms < 0 or self.observed_at_ms < 0:
            raise ValueError("recovery attempt/timestamps are invalid")
