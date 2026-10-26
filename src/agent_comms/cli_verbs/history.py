from __future__ import annotations

from agent_comms.channel_targets import CommandTarget
from agent_comms.channels import AnyChannel
from agent_comms.cli_command import CliCommand
from agent_comms.comms import Comms
from agent_comms.field_codec import Argument
from agent_comms.history_result import HistoryPageResult
from agent_comms.message_page import PageBounds
from agent_comms.page_traversal import NewestCursor
from agent_comms.page_traversal import PageCursor
from agent_comms.read_basis import AnonymousViewer
from agent_comms.read_basis import DisplayViewer
from dataclasses import dataclass
from typing import Annotated
import json


@dataclass(frozen=True, kw_only=True)
class HistoryCliCommand(CliCommand):
    help = "Newest-first bounded history of a named target"
    target: Annotated[
        str, Argument(flags=("target",), options={"nargs": "?", "help": "#channel, @peer or a peer's name"})
    ] = AnyChannel.address()
    me: Annotated[
        DisplayViewer,
        Argument(
            flags=("--as",), options={"help": "Participant, required only for DM history"}
        ),
    ] = AnonymousViewer()
    limit: Annotated[
        int,
        Argument(flags=("--limit",), options={"help": "Maximum messages in this page"}),
    ] = 20
    before: Annotated[
        PageCursor,
        Argument(
            flags=("--before",),
            options={"help": "Exclusive older-page cursor (JSON)", "type": json.loads},
        ),
    ] = NewestCursor()

    def apply(self, ctx: Comms) -> HistoryPageResult:
        target = CommandTarget.named(self.target)
        bounds = PageBounds(self.before.older(), self.limit)
        page = ctx.views.history_page(target, bounds, viewer=self.me)
        return target.history_result(ctx.bus.require_channel, tuple(reversed(page.messages)), page.has_older)
