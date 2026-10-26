"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.field_codec import Argument
from agent_comms.field_codec import TargetBound
from dataclasses import dataclass
from typing import Annotated


@dataclass(frozen=True, kw_only=True)
class NamedCliCommand(CliCommand):
    name: Annotated[str, TargetBound(), Argument(flags=("--name",))]
