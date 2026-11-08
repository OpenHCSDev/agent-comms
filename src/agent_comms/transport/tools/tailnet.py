"""Tailnet operations run where the selected Core has its network access."""

from dataclasses import dataclass
from typing import Annotated

from agent_comms.connector_profile import UiGrantId
from agent_comms.tailnet_buses import PeerBus, PeerHoldsBus
from agent_comms.tailnet_profiles import AskedPeer
from agent_comms.tool_result import ToolResult
from agent_comms.transport.request import ReadRequest, ToolRequest, tool_field


@dataclass(frozen=True)
class TailnetBusesRead(ToolResult):
    peers: tuple[PeerBus, ...]


@dataclass(frozen=True, kw_only=True)
class CommsTailnetBusesTool(ReadRequest[TailnetBusesRead]):
    label = "Discover buses"
    description = "Discover buses from Tailscale on Core's host, not on the terminal."


@dataclass(frozen=True, kw_only=True)
class CommsTailnetProfileTool(ToolRequest[AskedPeer]):
    label = "Open tailnet bus"
    description = "Ask a discovered peer for a UI profile; requires a human UI caller."
    found: Annotated[PeerHoldsBus, *tool_field("The discovered bus")]
    previous: Annotated[tuple[UiGrantId, ...], *tool_field("Grants this terminal replaces")] = ()
