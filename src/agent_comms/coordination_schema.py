"""Coordinator DDL assembled from the canonical TypedTable family."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from functools import cache
from typing import Final

import agent_comms.coordination_tables as coordination_tables
from agent_comms.assignment_states import AssignmentState
from agent_comms.attempt_states import AttemptState, RetryPredecessor
from agent_comms.coordination_errors import SchemaVersionError
from agent_comms.declared_family import DeclaredFamily
from agent_comms.execution_states import (
    AttemptExecution,
    EngagedExecution,
    ExecutionState,
    InterruptedExecution,
)
from agent_comms.lifecycle_capability import Failed, Successful, Terminal
from agent_comms.obligation_states import ResponseState
from agent_comms.recovery_states import RecoveryCondition
from agent_comms.typed_table import (
    DurableTables,
    SharedFileTables,
    TypedTable,
    sql_literal,
)
from agent_comms.wake_policy import WakePolicy

COORDINATION_SNAPSHOT_VERSION: Final = 3


class CoordinatorTable(SharedFileTables, DurableTables):
    """The coordinator schema; cases and transitions come from their owners.

    The live objects are compared to these declarations; nothing seals a copy of them.
    """

    schema_error = SchemaVersionError

    @classmethod
    def schema_rows(cls) -> tuple[type[TypedTable], ...]:
        return tuple(cls.declarations())

    @classmethod
    def declarations(cls) -> tuple[type[TypedTable], ...]:
        return TypedTable.discover_members(coordination_tables, cls)

    @classmethod
    def configure_connection(cls, connection: sqlite3.Connection) -> None:
        """A row may install connection behavior required by its own SQL."""


class CoordinatorRow(CoordinatorTable, TypedTable, declared_name=None):
    """Coordinator rows apply their state families to the SQL they declare."""

    @classmethod
    def schema_objects(cls, *, if_not_exists: bool = False) -> dict[str, str]:
        context = _schema_context(*(family.members_with(family) for family in _CONTEXT_FAMILIES))
        return {
            name: sql.format(**context)
            for name, sql in super().schema_objects(if_not_exists=if_not_exists).items()
        }


def _sql_values(names: Iterable[str]) -> str:
    return ",".join(sql_literal(name) for name in names)


_CONTEXT_FAMILIES: Final = (
    ExecutionState, AttemptState, AssignmentState, WakePolicy, ResponseState, RecoveryCondition,
)


@cache
def _schema_context(*members: tuple[type[DeclaredFamily], ...]) -> dict[str, str]:
    """The SQL the families' members imply; keyed by the members, so a new one rebuilds it."""
    context = dict(
        execution_names=_sql_values(ExecutionState.names()),
        attempt_names=_sql_values(AttemptState.names()),
        assignment_names=_sql_values(AssignmentState.names()),
        wake_names=_sql_values(WakePolicy.names()),
        response_names=_sql_values(ResponseState.names()),
        recovery_names=_sql_values(RecoveryCondition.names()),
        terminal_attempt_names=_sql_values(
            member.declared_name for member in AttemptState.members_with(Terminal)
        ),
        failed_attempt_phases=_sql_values(
            member.declared_name for member in AttemptState.members_with(Failed)
        ),
        retry_attempt_phases=_sql_values(
            member.declared_name for member in AttemptState.members_with(RetryPredecessor)
        ),
        engaged_execution_names=_sql_values(
            member.declared_name for member in ExecutionState.members_with(EngagedExecution)
        ),
        unstarted_execution_names=_sql_values(
            member.declared_name for member in ExecutionState.members_with(ExecutionState)
            if member.unstarted
        ),
        required_attempt_execution_names=_sql_values(
            member.declared_name for member in ExecutionState.members_with(AttemptExecution)
        ),
        optional_attempt_execution_names=_sql_values(
            member.declared_name for member in ExecutionState.members_with(InterruptedExecution)
        ),
        startable_execution_names=_sql_values(
            member.declared_name for member in ExecutionState.members_with(ExecutionState)
            if member.starts_attempt
        ),
        successful_response_names=_sql_values(
            member.declared_name for member in ResponseState.members_with(Successful)
        ),
        retryable_response_names=_sql_values(
            member.declared_name for member in ResponseState.members_with(ResponseState)
            if member.retryable
        ),
        intent_response_names=_sql_values(
            member.declared_name for member in ResponseState.members_with(ResponseState)
            if member.allows_intent
        ),
        required_intent_response_names=_sql_values(
            member.declared_name for member in ResponseState.members_with(ResponseState)
            if member.requires_intent
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
