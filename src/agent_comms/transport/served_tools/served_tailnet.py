"""Core owns network access; the terminal owns the profiles it holds."""

from dataclasses import replace

from agent_comms.comms import Comms
from agent_comms.transport.connector_profile import UiGrantId
from agent_comms.transport.connector_store import UiAccess
from agent_comms.transport.tailnet_buses import PeerBus, TailnetBuses
from agent_comms.transport.tailnet_profiles import AskedPeer, TailnetProfiles
from agent_comms.transport.tool_caller import ToolCaller
from agent_comms.transport.tool_request import (
    CoreRead,
    HostArea,
    HumanToolRequest,
)
from agent_comms.transport.tools.tailnet import (
    CommsTailnetBusesTool,
    CommsTailnetProfileTool,
    TailnetBusesRead,
)


class HostTailnetProfiles(TailnetProfiles):
    """The ask: one ssh call from Core's host to a peer's own connector."""

    @classmethod
    def ask_command(cls, found: PeerBus) -> tuple[str, ...]:
        """Its own connector decides: this says nothing about the peer's root or endpoint,
        because the machine being asked owns both."""
        return (
            *cls.probe_command(found.peer)[:-1],
            f"{cls.connector_cli} offer-ui --access {UiAccess.declared_name}",
        )

    @classmethod
    def ask(cls, found: PeerBus, previous: tuple[UiGrantId, ...] = ()) -> AskedPeer:
        """Ask on Core's host; the profile value crosses back to its recipient."""
        status, output = cls.run(cls.ask_command(found))
        answer = cls.asked(found, status, output)
        answer.replace_previous(previous)
        return answer


class ServedCommsTailnetBusesTool(HostArea, CoreRead[TailnetBusesRead], CommsTailnetBusesTool):
    def apply(self, comms: Comms, caller: ToolCaller) -> TailnetBusesRead:
        return TailnetBusesRead(TailnetBuses.seen())


class ServedCommsTailnetProfileTool(HostArea, HumanToolRequest[AskedPeer], CommsTailnetProfileTool):
    def answered(self, comms: Comms) -> AskedPeer:
        """Core ssh's to a peer its own tailnet reports, whoever named it and however.

        A remote caller cannot turn a row into arbitrary host-side SSH, and a typed
        address is a row: each member says which peer it means among these, so one guard
        covers a discovered row and a hand-typed one without the caller choosing which
        check applies to it.
        """
        found = replace(self.found, peer=self.found.on_tailnet(TailnetBuses.peers()))
        return HostTailnetProfiles.ask(found, self.previous)
