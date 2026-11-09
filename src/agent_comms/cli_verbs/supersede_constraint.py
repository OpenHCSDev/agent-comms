from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.cli_verbs.cli_commands import (
    ConstraintSupersededResult,
    WorktreeArgument,
    _message_reference,
)
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.messaging.channel_targets import CommandTarget
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.task_sources import CorrectionTaskChange, UserTaskSupersession
from agent_comms.nominal.field_codec import Argument, Normalize


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
