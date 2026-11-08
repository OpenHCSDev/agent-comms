"""Core owns network access; the terminal owns the profiles it holds."""

from agent_comms.comms import Comms
from agent_comms.tailnet_buses import TailnetBuses
from agent_comms.tailnet_profiles import AskedPeer, TailnetProfiles
from agent_comms.tool_caller import ToolCaller
from agent_comms.tool_request import CoreRead, HumanToolRequest
from agent_comms.transport.tools.tailnet import (
    CommsTailnetBusesTool, CommsTailnetProfileTool, TailnetBusesRead,
)


class ServedCommsTailnetBusesTool(CoreRead[TailnetBusesRead], CommsTailnetBusesTool):
    def apply(self, comms: Comms, caller: ToolCaller) -> TailnetBusesRead:
        return TailnetBusesRead(TailnetBuses.seen())


class ServedCommsTailnetProfileTool(HumanToolRequest[AskedPeer], CommsTailnetProfileTool):
    def answered(self, comms: Comms) -> AskedPeer:
        # A remote caller cannot turn a discovered row into arbitrary host-side SSH.
        if self.found.peer not in TailnetBuses.peers():
            raise ValueError("This bus is no longer a peer on Core's tailnet; discover again.")
        return TailnetProfiles.ask(self.found, self.previous)
