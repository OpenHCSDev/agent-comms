from __future__ import annotations

from agent_comms.channel_targets import CommandTarget
from agent_comms.cli_commands import UserMessageSentResult
from agent_comms.cli_commands import WorktreeArgument
from agent_comms.cli_result import CliResult
from agent_comms.cli_support.publishing import PublishingCliCommand
from agent_comms.comms import Comms
from agent_comms.field_codec import Argument
from dataclasses import dataclass
from typing import Annotated


@dataclass(frozen=True, kw_only=True)
class UserSendCliCommand(WorktreeArgument, PublishingCliCommand, declared_name="user-send"):
    help = "Send as the human user to a thread (DM), #channel, or #all"
    target: Annotated[str, Argument(flags=("--to",))]
    body: Annotated[str, Argument(flags=("--body",))]

    def publish(self, ctx: Comms) -> CliResult:
        message = ctx.messaging.send_user_message(CommandTarget.named(self.target), self.body, worktree=self.worktree)
        return UserMessageSentResult(message.reference)
