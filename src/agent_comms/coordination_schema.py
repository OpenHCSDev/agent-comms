"""Coordinator DDL assembled from the canonical TypedTable family."""

from __future__ import annotations

from functools import cache
from importlib import import_module
from pkgutil import iter_modules
from typing import Final

import agent_comms.coordination_tables as coordination_tables

from agent_comms.assignment_states import AssignmentState, EngagedAssignment
from agent_comms.attempt_states import AttemptState, RetryPredecessor
from agent_comms.execution_states import AttemptExecution, ExecutionState, InterruptedExecution
from agent_comms.lifecycle_capability import Failed, Successful, Terminal
from agent_comms.obligation_states import ResponseState
from agent_comms.recovery_states import RecoveryCondition
from agent_comms.coordination_errors import SchemaVersionError
from agent_comms.typed_table import (
    DurableTables,
    SharedFileTables,
    TypedTable,
)
from agent_comms.wake_policy import WakePolicy

COORDINATION_SNAPSHOT_VERSION: Final = 3


class CoordinatorTable(SharedFileTables, DurableTables):
    """The coordinator schema; cases and transitions come from their owners.

    The live objects are compared to these declarations; nothing seals a copy of them.
    """

    schema_error = SchemaVersionError

    @classmethod
    def schema_rows(cls):
        return tuple(cls.declarations())

    @classmethod
    def declarations(cls):
        for module in iter_modules(coordination_tables.__path__, coordination_tables.__name__ + "."):
            import_module(module.name)
        return TypedTable.members_with(cls)

    @classmethod
    def configure_connection(cls, connection) -> None:
        """A row may install connection behavior required by its own SQL."""

    @classmethod
    def schema_objects(cls, *, if_not_exists: bool = False):
        context = _schema_context(*(family.members_with(family) for family in _CONTEXT_FAMILIES))
        return {
            name: sql.format(**context)
            for name, sql in super().schema_objects(if_not_exists=if_not_exists).items()
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


_CONTEXT_FAMILIES: Final = (
    ExecutionState, AttemptState, AssignmentState, WakePolicy, ResponseState, RecoveryCondition,
)


@cache
def _schema_context(*members):
    """The SQL the families' members imply; keyed by the members, so a new one rebuilds it."""
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
        terminal_attempt_names=_sql_values(
            member.declared_name for member in AttemptState.members_with(Terminal)
        ),
        failed_attempt_phases=_sql_values(
            member.declared_name for member in AttemptState.members_with(Failed)
        ),
        retry_attempt_phases=_sql_values(
            member.declared_name for member in AttemptState.members_with(RetryPredecessor)
        ),
        engaged_execution_names=_sql_members(
            ExecutionState,
            lambda member: member.assignment_state() in AssignmentState.members_with(EngagedAssignment)
        ),
        unstarted_execution_names=_sql_members(ExecutionState, lambda member: member.unstarted),
        required_attempt_execution_names=_sql_values(
            member.declared_name for member in ExecutionState.members_with(AttemptExecution)
        ),
        optional_attempt_execution_names=_sql_values(
            member.declared_name for member in ExecutionState.members_with(InterruptedExecution)
        ),
        startable_execution_names=_sql_members(
            ExecutionState, lambda member: member.starts_attempt
        ),
        successful_response_names=_sql_values(
            member.declared_name for member in ResponseState.members_with(Successful)
        ),
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
