from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.nominal.field_codec import Argument, TargetBound


@dataclass(frozen=True, kw_only=True)
class NamedCliCommand(CliCommand):
    name: Annotated[str, TargetBound(), Argument(flags=("--name",))]
