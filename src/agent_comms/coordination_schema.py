"""Coordinator DDL assembled from the canonical TypedTable family."""

from __future__ import annotations

from typing import Final

from agent_comms.assignment_states import AssignmentState
from agent_comms.attempt_states import AttemptState
from agent_comms.execution_states import ExecutionState
from agent_comms.obligation_states import ResponseState
from agent_comms.recovery_states import RecoveryCondition
from agent_comms.typed_table import (
    TypedTable,
)
from agent_comms.wake_policy import WakePolicy

COORDINATION_SCHEMA_VERSION: Final = 9


COORDINATION_SNAPSHOT_VERSION: Final = 3


class CoordinatorTable:
    """Capability for the coordinator schema; cases and transitions come from their owners."""

    @classmethod
    def schema_objects(cls):
        return {
            name: sql.format(**_schema_context()) for name, sql in super().schema_objects().items()
        }


def _sql_values(names):
    return ",".join("'" + name.replace("'", "''") + "'" for name in names)


def _sql_members(family, predicate=lambda member: True):
    return _sql_values(
        member.declared_name for member in family.members_with(family) if predicate(member)
    )


def _sql_edges(family, column):
    return (
        " OR ".join(
            f"({column.format(row='OLD')} = {_sql_values((name,))} "
            f"AND {column.format(row='NEW')} IN ({_sql_values(sorted(edges))}))"
            for name, edges in family.transition_table().items()
            if edges
        )
        or "0"
    )


def _schema_context():
    context = dict(
        execution_names=_sql_members(ExecutionState),
        attempt_names=_sql_members(AttemptState),
        assignment_names=_sql_members(AssignmentState),
        wake_names=_sql_members(WakePolicy),
        response_names=_sql_members(ResponseState),
        recovery_names=_sql_members(RecoveryCondition),
        execution_edges=_sql_edges(ExecutionState, "json_extract({row}.lifecycle, '$.kind')"),
        attempt_edges=_sql_edges(AttemptState, "json_extract({row}.lifecycle, '$.kind')"),
        assignment_edges=_sql_edges(AssignmentState, "json_extract({row}.lifecycle, '$.kind')"),
        obligation_edges=_sql_edges(ResponseState, "json_extract({row}.lifecycle, '$.kind')"),
        terminal_attempt_names=_sql_members(AttemptState, lambda member: member.terminal),
        engaged_execution_names=_sql_members(
            ExecutionState, lambda member: member.assignment_state().engaged
        ),
        unstarted_execution_names=_sql_members(ExecutionState, lambda member: member.unstarted),
        required_attempt_execution_names=_sql_members(
            ExecutionState, lambda member: member.active or member.completed
        ),
        optional_attempt_execution_names=_sql_members(
            ExecutionState, lambda member: member.retry or member.failed
        ),
        startable_execution_names=_sql_members(
            ExecutionState, lambda member: member.starts_attempt
        ),
        successful_response_names=_sql_members(ResponseState, lambda member: member.successful),
        retryable_response_names=_sql_members(ResponseState, lambda member: member.retryable),
        intent_response_names=_sql_members(ResponseState, lambda member: member.allows_intent),
        required_intent_response_names=_sql_members(
            ResponseState, lambda member: member.requires_intent
        ),
    )

    for suffix, expression in (
        ("", "lifecycle"),
        ("_new", "NEW.lifecycle"),
        ("_old", "OLD.lifecycle"),
    ):
        context["assignment_mode" + suffix] = (
            "CASE json_extract("
            + expression
            + ", '$.kind') "
            + " ".join(
                f"WHEN {_sql_values((member.declared_name,))} THEN {member.mode_expression(expression)}"
                for member in AssignmentState.members_with(AssignmentState)
            )
            + " END"
        )
        context["assignment_verdict" + suffix] = (
            "CASE json_extract("
            + expression
            + ", '$.kind') "
            + " ".join(
                f"WHEN {_sql_values((member.declared_name,))} THEN {member.verdict_expression(expression)}"
                for member in AssignmentState.members_with(AssignmentState)
            )
            + " END"
        )
    context["assignment_binding"] = (
        "CASE disposition "
        + " ".join(
            f"WHEN {_sql_values((member.declared_name,))} THEN ({member.binding_expression()})"
            for member in AssignmentState.members_with(AssignmentState)
        )
        + " END"
    )
    return context


def coordinator_schema():
    from agent_comms import coordination_tables  # noqa: F401 - load declared table family

    tables = "\n".join(
        statement + ";"
        for row_type in TypedTable.members_with(CoordinatorTable)
        for statement in row_type.schema_objects().values()
    )
    return tables + "\n" + ("""CREATE VIEW retry_disposition_basis AS
SELECT e.execution_id,
  CASE WHEN e.current_attempt_ordinal IS NOT NULL
     AND e.current_attempt_ordinal < e.max_attempts
     AND EXISTS (SELECT 1 FROM replay_assessments r
       WHERE r.execution_id = e.execution_id AND r.retry_authorized = 1)
     AND (e.origin != 'wire' OR (EXISTS (SELECT 1 FROM obligations o
       WHERE o.execution_id=e.execution_id) AND NOT EXISTS (SELECT 1 FROM obligations o
       WHERE o.execution_id=e.execution_id AND o.retryable != 1)))
  THEN 1 ELSE 0 END AS authorized
FROM executions e;""").format(**_schema_context())
