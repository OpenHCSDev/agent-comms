from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import InboxResult
from agent_comms.cli_commands import ThreadArgument
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument
from agent_comms.message_reference import NewestUnreadStart
from agent_comms.page_traversal import NewestCursor
from agent_comms.page_traversal import PageCursor
from dataclasses import dataclass
from typing import Annotated
import json


@dataclass(frozen=True, kw_only=True)
class InboxCliCommand(ThreadArgument, CliCommand):
    help = "Undelivered messages for a thread, newest first"
    before: Annotated[
        PageCursor,
        Argument(
            flags=("--before",),
            options={"help": "Exclusive older-page cursor (JSON)", "type": json.loads},
        ),
    ] = NewestCursor()

    def apply(self, ctx: Comms) -> CliResult:
        inbox = ctx.bus.inbox(self.thread, after=NewestUnreadStart(self.before))
        shown = tuple(reversed(inbox.page.messages))
        return InboxResult(shown, inbox.page.has_older, len(shown) + inbox.remaining)
