"""Coordinator declarations inherit restrictions and install through their schema family."""

from dataclasses import dataclass, replace
from typing import Annotated

import pytest

from agent_comms.coordination.database import CoordinationStore
from agent_comms.coordination.schema import CoordinatorRow
from agent_comms.coordination.tables.executions import ExecutionReference
from agent_comms.coordination.tables.recovery import RecoveryAudit
from agent_comms.storage.typed_table import Column, ForeignKey

pytestmark = pytest.mark.refactor_guard


def test_recovery_audit_inherits_changes_to_execution_restriction():
    assert issubclass(RecoveryAudit, ExecutionReference)
    restriction = replace(ExecutionReference.references()[0], deferred=True)

    class DeferredExecutionReference(ExecutionReference):
        @classmethod
        def references(cls) -> tuple[ForeignKey, ...]:
            return (restriction,)

    class RecoveryAuditProbe(
        DeferredExecutionReference,
        RecoveryAudit,
        declared_name="recovery_execution_restriction_probe",
    ):
        pass

    try:
        assert restriction.sql(RecoveryAuditProbe) in RecoveryAuditProbe.ddl()[0]
    finally:
        RecoveryAuditProbe.withdraw()


def test_new_declared_table_is_installed_without_schema_roster_edit(tmp_path):
    @dataclass(frozen=True)
    class ProofRow(CoordinatorRow, declared_name="coordinator_extension_probe"):
        identity: Annotated[str, Column(primary_key=True)]
        value: int

    try:
        database = tmp_path / "coordination.sqlite3"
        with CoordinationStore(database) as store:
            row = ProofRow("new-declaration", 7)
            row.insert(store._connection)
            assert ProofRow.one(store._connection, identity=row.identity) == row
        with CoordinationStore(database) as reopened:
            assert ProofRow.one(reopened._connection, identity=row.identity) == row
    finally:
        ProofRow.withdraw()
