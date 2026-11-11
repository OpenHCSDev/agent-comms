from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.cli_verbs.cli_commands import _tags
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.execution.check_root import CheckRoot
from agent_comms.nominal.field_codec import Argument, Normalize, PathText


@dataclass(frozen=True, kw_only=True)
class CheckRootCliCommand(CliCommand, declared_name="check-root"):
    help = "Run a command against selected recent history, then remove its private root"
    threads: Annotated[
        tuple[str, ...],
        Normalize(_tags),
        Argument(options={"help": "Comma-separated source thread names"}),
    ]
    into: Annotated[
        Path,
        PathText,
        Argument(options={"help": "New directory under your agent scratch directory"}),
    ]
    argv: Annotated[
        tuple[str, ...],
        Argument(
            flags=("argv",),
            options={
                "nargs": argparse.REMAINDER,
                "help": "-- COMMAND ARGS; use {root} for the private root argument",
            },
        ),
    ]

    def apply(self, ctx: Comms) -> CliResult:
        return CheckRoot(ctx, self.threads, self.into).run(self.argv)
