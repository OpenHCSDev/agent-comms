from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated

from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.comms import Comms
from agent_comms.messaging.channel_targets import CommandTarget
from agent_comms.messaging.channels import AnyChannel
from agent_comms.messaging.history_result import DmHistoryPageResult, HistoryPageResult
from agent_comms.messaging.message_page import PageBounds
from agent_comms.messaging.page_traversal import NewestCursor, PageCursor
from agent_comms.messaging.read_basis import AnonymousViewer, DisplayViewer
from agent_comms.nominal.field_codec import Argument


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
        return target.history_result(
            ctx.bus.require_channel, DmHistoryPageResult, tuple(reversed(page.messages)), page.has_older,
        )
