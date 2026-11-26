"""The recipient and worktree every original-constraint pin verb shares."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from agent_comms.cli_verbs.cli_verb import CliCommand
from agent_comms.cli_verbs.cli_commands import WorktreeArgument
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.execution.input_origin import InputProvenance
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.nominal.field_codec import Argument


@dataclass(frozen=True)
class ConstraintPinnedResult(CliResult):
    pin: MessageReference
    source: MessageReference | InputProvenance


@dataclass(frozen=True)
class ConstraintSupersededResult(CliResult):
    supersession: MessageReference


@dataclass(frozen=True)
class ConstraintDroppedResult(CliResult):
    drop: MessageReference


@dataclass(frozen=True, kw_only=True)
class OriginalConstraintPin(WorktreeArgument, CliCommand, declared_name=None):
    """Each pin verb declares its own original source and how the bus pins it."""

    thread: Annotated[str, Argument(flags=("thread",))]
