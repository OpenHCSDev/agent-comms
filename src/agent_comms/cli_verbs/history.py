from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, TextIO

from agent_comms.cli_verbs.cli_verb import BusOpener, CliBus, CliCommand
from agent_comms.installer.active_route import CommsRoute
from agent_comms.messaging.channel_targets import CommandTarget
from agent_comms.messaging.channels import AnyChannel
from agent_comms.messaging.historical_views import HistoryView
from agent_comms.messaging.history_result import DmHistoryPageResult, HistoryPageResult
from agent_comms.messaging.message_bus import MessageBus
from agent_comms.messaging.message_page import PageBounds
from agent_comms.messaging.page_traversal import NewestCursor, PageCursor
from agent_comms.messaging.publisher import OwnerPublisher
from agent_comms.messaging.read_basis import AnonymousViewer, DisplayViewer
from agent_comms.nominal.field_codec import Argument
from agent_comms.planning.goal_snapshot_reader import GoalSnapshotReader
from agent_comms.presentation import BusPresentation
from agent_comms.registry.catalog_store import ChannelCatalog
from agent_comms.registry.route_client import ActivityClient


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
    sender: Annotated[
        str,
        Argument(
            flags=("--from",),
            options={"help": "Filter to one sender name, or 'human'/'owner' for verbatim owner turns"},
        ),
    ] = ""

    def _read(self, presentation: BusPresentation) -> HistoryPageResult:
        target = CommandTarget.named(self.target)
        bounds = PageBounds(self.before.older(), self.limit)
        page = HistoryView.read_page(target, presentation, bounds, viewer=self.me, sender=self.sender)
        return target.history_result(
            presentation.bus.require_channel,
            DmHistoryPageResult,
            tuple(reversed(page.messages)),
            page.has_older,
        )

    def apply(self, ctx: CliBus) -> HistoryPageResult:
        return self._read(ctx.views.presentation)

    def execute(self, route: CommsRoute, output: TextIO, open_bus: BusOpener) -> int:
        with ActivityClient.client(route) as client:
            bus = MessageBus(client.root, client.registry, OwnerPublisher)
            goals = GoalSnapshotReader(client.root, client.agents)
            presentation = BusPresentation(
                bus, client.registry, ChannelCatalog.at(client.root), client.agents, goals,
            )
            return self._read(presentation).write(output)
