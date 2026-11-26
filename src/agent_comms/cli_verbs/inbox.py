from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated

from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.cli_verbs.cli_commands import ThreadArgument
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.messaging.history_result import HistoryPageResult
from agent_comms.messaging.message_reference import NewestUnreadStart
from agent_comms.messaging.page_traversal import NewestCursor, PageCursor
from agent_comms.nominal.field_codec import Argument


@dataclass(frozen=True)
class InboxResult(HistoryPageResult):
    """The newest unread page, newest first, and how many unread messages there are in all."""

    pending: int

    def to_wire(self) -> dict[str, object]:
        return {**super().to_wire(), "pending": self.pending}


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
