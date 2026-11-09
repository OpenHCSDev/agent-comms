"""Tailnet operations run where the selected Core has its network access."""

from dataclasses import dataclass
from typing import Annotated

from agent_comms.transport.connector_profile import UiGrantId
from agent_comms.transport.request import ReadRequest, ToolRequest, tool_field
from agent_comms.transport.tailnet_buses import PeerBus
from agent_comms.transport.tailnet_profiles import AskedPeer
from agent_comms.transport.tool_result import ToolResult


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
    found: Annotated[PeerBus, *tool_field("The bus to ask: one discovered, or one typed in")]
    previous: Annotated[tuple[UiGrantId, ...], *tool_field("Grants this terminal replaces")] = ()
