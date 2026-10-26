from __future__ import annotations

from agent_comms.channel_targets import CommandTarget
from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import ConstraintSupersededResult
from agent_comms.cli_commands import WorktreeArgument
from agent_comms.cli_commands import _message_reference
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.field_codec import Argument
from agent_comms.field_codec import Normalize
from agent_comms.message_reference import MessageReference
from agent_comms.task_sources import CorrectionTaskChange
from agent_comms.task_sources import UserTaskSupersession
from dataclasses import dataclass
from typing import Annotated


@dataclass(frozen=True, kw_only=True)
class SupersedeConstraintCliCommand(
    WorktreeArgument, CliCommand, declared_name="supersede-constraint"
):
    help = "Publish a human's exact replacement for an authored constraint"
    thread: Annotated[str, Argument(flags=("thread",))]
    source: Annotated[
        MessageReference,
        Normalize(_message_reference),
        Argument(flags=("--source",)),
    ]
    body: Annotated[str, Argument(flags=("--body",))]

    def apply(self, ctx: Comms) -> CliResult:
        message = ctx.messaging.send_user_message(CommandTarget.named(self.thread), self.body, worktree=self.worktree,
            task=UserTaskSupersession(CorrectionTaskChange(self.source)))
        return ConstraintSupersededResult(message.reference)
