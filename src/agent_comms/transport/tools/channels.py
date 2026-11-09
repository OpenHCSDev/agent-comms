"""The channel listing: the named tag views and their member threads.

`CommsChannelsTool` is a read whose reply is `ChannelListing`; both live here because the
listing is this read's only publication.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_comms.display_order import ChannelSort
from agent_comms.messaging.channels import SavedView
from agent_comms.presentation import ChannelView
from agent_comms.transport.tool_caller import ToolCaller
from agent_comms.transport.tool_request import NativeToolRequest, ReadTool
from agent_comms.transport.tool_result import ToolResult

if TYPE_CHECKING:
    from agent_comms.comms import Comms


@dataclass(frozen=True)
class ChannelListing(ToolResult):
    channels: tuple[ChannelView, ...]
    views: tuple[SavedView, ...]
    order: type[ChannelSort]


@dataclass(frozen=True, kw_only=True)
class CommsChannelsTool(ReadTool, NativeToolRequest):
    label = "List channels"
    description = "List named tag views and their current member threads; #any includes everyone."

    def apply(self, comms: Comms, caller: ToolCaller) -> ChannelListing:
        catalog = comms.channels.catalog.read()
        return ChannelListing(comms.views.channel_views(), tuple(catalog.saved_views.values()),
                              catalog.list_order)
