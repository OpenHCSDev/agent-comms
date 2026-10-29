"""The recipient and worktree every original-constraint pin verb shares."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import WorktreeArgument
from agent_comms.nominal.field_codec import Argument


@dataclass(frozen=True, kw_only=True)
class OriginalConstraintPin(WorktreeArgument, CliCommand, declared_name=None):
    """Each pin verb declares its own original source and how the bus pins it."""

    thread: Annotated[str, Argument(flags=("thread",))]
