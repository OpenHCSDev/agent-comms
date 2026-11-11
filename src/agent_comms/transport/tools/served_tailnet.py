"""Core owns network access; the terminal owns the profiles it holds."""

from dataclasses import replace

from agent_comms.comms import Comms
from agent_comms.transport.tailnet_buses import TailnetBuses
from agent_comms.transport.tailnet_profiles import AskedPeer, TailnetProfiles
from agent_comms.transport.tool_caller import ToolCaller
from agent_comms.transport.tool_request import CoreRead, HumanToolRequest
from agent_comms.transport.tools.tailnet import (
    CommsTailnetBusesTool,
    CommsTailnetProfileTool,
    TailnetBusesRead,
)


class ServedCommsTailnetBusesTool(CoreRead[TailnetBusesRead], CommsTailnetBusesTool):
    def apply(self, comms: Comms, caller: ToolCaller) -> TailnetBusesRead:
        return TailnetBusesRead(TailnetBuses.seen())


class ServedCommsTailnetProfileTool(HumanToolRequest[AskedPeer], CommsTailnetProfileTool):
    def answered(self, comms: Comms) -> AskedPeer:
        """Core ssh's to a peer its own tailnet reports, whoever named it and however.

        A remote caller cannot turn a row into arbitrary host-side SSH, and a typed
        address is a row: each member says which peer it means among these, so one guard
        covers a discovered row and a hand-typed one without the caller choosing which
        check applies to it.
        """
        found = replace(self.found, peer=self.found.on_tailnet(TailnetBuses.peers()))
        return TailnetProfiles.ask(found, self.previous)
