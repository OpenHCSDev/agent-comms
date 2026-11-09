from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.cli_verbs.cli_commands import (
    ConstraintDroppedResult,
    WorktreeArgument,
    _message_reference,
)
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.messaging.channel_targets import CommandTarget
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.task_sources import CorrectionTaskChange, UserTaskDrop
from agent_comms.nominal.field_codec import Argument, Normalize


@dataclass(frozen=True, kw_only=True)
class DropConstraintCliCommand(WorktreeArgument, CliCommand, declared_name="drop-constraint"):
    help = "Retire a constraint by original reference, preserving its evidence"
    thread: Annotated[str, Argument(flags=("thread",))]
    source: Annotated[
        MessageReference,
        Normalize(_message_reference),
        Argument(flags=("--source",)),
    ]

    def apply(self, ctx: Comms) -> CliResult:

        message = ctx.messaging.send_user_message(CommandTarget.named(self.thread),
            f"Dropped constraint {self.source.seq}:{self.source.message_id}", worktree=self.worktree,
            task=UserTaskDrop(CorrectionTaskChange(self.source)))
        return ConstraintDroppedResult(message.reference)
