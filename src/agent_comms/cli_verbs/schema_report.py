"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.schema_report import StoreSchemaReport
from agent_comms.schema_report import StoreSchemaReports
from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class SchemaReportCliCommand(CliCommand, declared_name="schema-report"):
    help = "Report declared DDL differences in existing SQLite stores (including retained copies)"

    def apply(self, ctx: Comms) -> CliResult:
        return StoreSchemaReports(StoreSchemaReport.scan(ctx))
