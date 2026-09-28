"""Recovery rows own their SQL constraints and lifecycle relations."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import StrEnum

from agent_comms.coordination_contracts import (
    MAX_REASON_CODE_CHARS,
    MAX_SANITIZED_DETAIL_CHARS,
    require_bounded,
    require_nonempty,
    validate_execution_id,
)
from agent_comms.coordination_schema import CoordinatorTable
from agent_comms.recovery_states import RecoveryCondition
from agent_comms.typed_table import (
    Column,
    ForeignKey,
    Index,
    TypedTable,
)


class OwnerConnectivity(StrEnum):
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    OFFLINE = "offline"


class ACPClientConnectivity(StrEnum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"


@dataclass(frozen=True, slots=True)
class ConnectivityFacet(CoordinatorTable, TypedTable, declared_name="connectivity"):
    execution_id: str = dataclass_field(
        metadata={"snapshot_exclude": True, "sql": Column(primary_key=True)}
    )
    owner: OwnerConnectivity
    acp_client: ACPClientConnectivity
    revision: int = dataclass_field(metadata={"sql": Column(check="revision>0")})
    observed_at_ms: int = dataclass_field(metadata={"sql": Column(check="observed_at_ms>=0")})

    def __post_init__(self) -> None:
        validate_execution_id(self.execution_id)
        object.__setattr__(self, "owner", OwnerConnectivity(self.owner))
        object.__setattr__(self, "acp_client", ACPClientConnectivity(self.acp_client))
        if self.revision <= 0 or self.observed_at_ms < 0:
            raise ValueError("revision must be positive and observed_at_ms non-negative")

    @classmethod
    def references(cls):
        from agent_comms.coordination_tables.executions import ExecutionRecord

        return (
            ForeignKey(
                ("execution_id",),
                ExecutionRecord,
                ("execution_id",),
                deferred=False,
                on_delete="RESTRICT",
            ),
        )

    @classmethod
    def triggers(cls):
        return {
            "connectivity_observation_monotonic": (
                """CREATE TRIGGER connectivity_observation_monotonic
    BEFORE UPDATE ON connectivity
    WHEN NEW.execution_id IS NOT OLD.execution_id
     OR NEW.revision != OLD.revision + 1
     OR NEW.observed_at_ms < OLD.observed_at_ms
    BEGIN
        SELECT RAISE(ABORT, 'connectivity revision or observation regressed');
    END"""
            ),
            "connectivity_delete_frozen": (
                """CREATE TRIGGER connectivity_delete_frozen BEFORE DELETE ON connectivity BEGIN
        SELECT RAISE(ABORT, 'connectivity cannot be deleted');
    END"""
            ),
        }


@dataclass(frozen=True, slots=True)
class RecoveryAudit(CoordinatorTable, TypedTable):
    execution_id: str = dataclass_field(metadata={"snapshot_exclude": True})
    kind: type[RecoveryCondition] = dataclass_field(
        metadata={"sql": Column(check="kind IN ({recovery_names})")}
    )
    reason_code: str = dataclass_field(
        metadata={"sql": Column(check="length(reason_code) BETWEEN 1 AND 64")}
    )
    sanitized_detail: str | None = dataclass_field(
        metadata={
            "sql": Column(
                check=(
                    "sanitized_detail IS NULL OR length(sanitized_detail)<="
                    f"{MAX_SANITIZED_DETAIL_CHARS}"
                )
            )
        }
    )
    attempt: int = dataclass_field(metadata={"sql": Column(check="attempt>0")})
    elapsed_ms: int = dataclass_field(metadata={"sql": Column(check="elapsed_ms>=0")})
    observed_at_ms: int = dataclass_field(metadata={"sql": Column(check="observed_at_ms>=0")})
    audit_id: int | None = dataclass_field(
        default=None,
        metadata={"sql": Column(primary_key=True, auto_increment=True), "snapshot_exclude": True},
    )
    indexes = (Index(("execution_id", "audit_id")),)

    @classmethod
    def references(cls):
        from agent_comms.coordination_tables.attempts import AttemptRecord
        from agent_comms.coordination_tables.executions import ExecutionRecord

        return (
            ForeignKey(
                ("execution_id",),
                ExecutionRecord,
                ("execution_id",),
                deferred=False,
                on_delete="RESTRICT",
            ),
            ForeignKey(
                ("execution_id", "attempt"),
                AttemptRecord,
                ("execution_id", "attempt_ordinal"),
                deferred=False,
                on_delete=None,
            ),
        )

    @classmethod
    def triggers(cls):
        return {
            "recovery_audit_update_frozen": (
                """CREATE TRIGGER recovery_audit_update_frozen
BEFORE UPDATE ON recovery_audit
BEGIN
    SELECT RAISE(ABORT, 'recovery audit is append-only');
END"""
            ),
            "recovery_audit_delete_frozen": (
                """CREATE TRIGGER recovery_audit_delete_frozen
BEFORE DELETE ON recovery_audit
BEGIN
    SELECT RAISE(ABORT, 'recovery audit is append-only');
END"""
            ),
        }

    def __post_init__(self) -> None:
        validate_execution_id(self.execution_id)
        require_nonempty(self.reason_code, "reason_code")
        require_bounded(self.reason_code, "reason_code", MAX_REASON_CODE_CHARS)
        require_bounded(self.sanitized_detail, "sanitized_detail", MAX_SANITIZED_DETAIL_CHARS)
        if self.attempt <= 0 or self.elapsed_ms < 0 or self.observed_at_ms < 0:
            raise ValueError("recovery attempt/timestamps are invalid")
