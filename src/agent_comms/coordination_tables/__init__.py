"""Coordinator row declarations; schema assembly loads the complete family.

Importing one row must not import unrelated annotation and UI consumers.
"""

from agent_comms.typed_table import ForeignKey, GuardedAfterWrites


class ExecutionReference:
    """An execution-owned row cannot outlive its original execution."""

    @classmethod
    def references(cls) -> tuple[ForeignKey, ...]:
        from .executions import ExecutionRecord

        return (ForeignKey(("execution_id",), ExecutionRecord, ("execution_id",), on_delete="RESTRICT"),)


class RetryPartition(GuardedAfterWrites):
    """A failed execution cannot retain authorization to retry."""

    after_write_condition = """EXISTS (SELECT 1 FROM retry_disposition_basis b JOIN executions e ON e.execution_id = b.execution_id WHERE
        b.execution_id = NEW.execution_id AND b.authorized = 1 AND e.status = 'failed' AND e.current_attempt_ordinal
        IS NOT NULL)"""
