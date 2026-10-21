"""Recovery rows own their SQL constraints and lifecycle relations."""

from __future__ import annotations

from typing import Annotated
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import StrEnum

from agent_comms import field_codec
from agent_comms.coordination_contracts import (
    MAX_SANITIZED_DETAIL_CHARS,
    validate_execution_id,
)
from agent_comms.coordination_schema import CoordinatorTable
from agent_comms.coordination_tables.executions import ExecutionReference
from agent_comms.recovery_states import RecoveryCondition
from agent_comms.reason_codes import GivenReasonCode, RecoveryDetail
from agent_comms.typed_table import (
    Column,
    ForeignKey,
    FrozenColumns,
    ImmutableRows,
    IncrementingColumn,
    Index,
    MonotoneColumns,
    NoDelete,
    NonDecreasing,
    RowId,
    TextStorage,
    TypedTable,
    UnallocatedRowId,
)


class OwnerConnectivity(StrEnum):
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    OFFLINE = "offline"


class ACPClientConnectivity(StrEnum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"


@dataclass(frozen=True, slots=True)
class ConnectivityFacet(IncrementingColumn, MonotoneColumns, ExecutionReference, FrozenColumns, NoDelete, CoordinatorTable, TypedTable, declared_name="connectivity"):

    column_orders = (NonDecreasing(('observed_at_ms',)),)

    immutable_columns = ('execution_id',)

    execution_id: Annotated[str, field_codec.SnapshotExclude(), Column(primary_key=True)]
    owner: OwnerConnectivity
    acp_client: ACPClientConnectivity
    revision: Annotated[int, Column(check="revision>0")]
    observed_at_ms: Annotated[int, Column(check="observed_at_ms>=0")]

    def __post_init__(self) -> None:
        validate_execution_id(self.execution_id)
        object.__setattr__(self, "owner", OwnerConnectivity(self.owner))
        object.__setattr__(self, "acp_client", ACPClientConnectivity(self.acp_client))
        if self.revision <= 0 or self.observed_at_ms < 0:
            raise ValueError("revision must be positive and observed_at_ms non-negative")

@dataclass(frozen=True, slots=True)
class RecoveryAudit(ImmutableRows, CoordinatorTable, TypedTable):
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
    def references(cls):
        from agent_comms.coordination_tables.attempts import AttemptRecord
        from agent_comms.coordination_tables.executions import ExecutionRecord

        return (
            ForeignKey(("execution_id",), ExecutionRecord, ("execution_id",), on_delete="RESTRICT"),
            ForeignKey(
                ("execution_id", "attempt"), AttemptRecord, ("execution_id", "attempt_ordinal")
            ),
        )

    def __post_init__(self) -> None:
        validate_execution_id(self.execution_id)
        if self.attempt <= 0 or self.elapsed_ms < 0 or self.observed_at_ms < 0:
            raise ValueError("recovery attempt/timestamps are invalid")
