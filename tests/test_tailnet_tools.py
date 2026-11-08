"""The same typed operations execute on Core, regardless of the terminal's OS."""

import subprocess

import pytest
from bus_record_fixture import prepare_native_comms
from agent_comms_hooks import ToolRefusedError

from agent_comms.connector_caller import UiCaller, UiReadCaller
from agent_comms.core_access import LocalCoreAccess
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.tailnet_buses import (
    PeerBus, PeerHoldsBus, PeerOffline, PeerUnreachable, PeerWithoutBus, TailnetBuses, TailnetPeer,
)
from agent_comms.tailnet_profiles import AskedPeer, PeerRefusedProfile, TailnetProfiles
from agent_comms.tool_request import ServedRequest
from agent_comms.transport.tools.tailnet import (
    CommsTailnetBusesTool, CommsTailnetProfileTool, TailnetBusesRead,
)


PEER = TailnetPeer("a-discovered-peer", "100.64.1.2", True)
BUS = PeerHoldsBus(PEER, 7)


def test_every_discovery_outcome_crosses_the_existing_wire():
    # Membership, including future members, belongs to PeerBus, not a union at the RPC.
    outcomes = (BUS, PeerOffline(PEER), PeerUnreachable(PEER, "SSH refused"),
                PeerWithoutBus(PEER, "CLI absent"))
    assert {type(entry) for entry in outcomes} == set(PeerBus.members_with(PeerBus))
    result = TailnetBusesRead(outcomes)
    assert FieldCodec.decode(TailnetBusesRead, FieldCodec.encode(result)) == result


def test_the_selected_core_performs_discovery(comms, monkeypatch):
    monkeypatch.setattr(TailnetBuses, "seen", classmethod(lambda cls: (BUS,)))
    assert LocalCoreAccess(comms).call(CommsTailnetBusesTool()) == TailnetBusesRead((BUS,))


def test_a_read_only_ui_cannot_issue_profiles(comms, monkeypatch):
    request = ServedRequest.serving(CommsTailnetProfileTool(found=BUS))
    prepare_native_comms(comms)
    namespace = comms.registry.store.privacy().namespace
    with pytest.raises(ToolRefusedError, match="reads only"):
        UiReadCaller(namespace).invoke(request, comms)


def test_profile_requests_use_core_membership_and_return_values(comms, monkeypatch):
    monkeypatch.setattr(TailnetBuses, "peers", classmethod(lambda cls: (PEER,)))
    refused = PeerRefusedProfile(BUS, "peer's own refusal")
    calls = []
    monkeypatch.setattr(TailnetProfiles, "ask", classmethod(
        lambda cls, found, previous: calls.append((found, previous)) or refused))
    request = ServedRequest.serving(CommsTailnetProfileTool(found=BUS))
    prepare_native_comms(comms)
    namespace = comms.registry.store.privacy().namespace
    answer = UiCaller(namespace).invoke(request, comms)
    assert FieldCodec.decode(AskedPeer, FieldCodec.encode(answer)) == refused
    assert calls == [(BUS, ())]
    monkeypatch.setattr(TailnetBuses, "peers", classmethod(lambda cls: ()))
    with pytest.raises(ValueError, match="no longer a peer"):
        UiCaller(namespace).invoke(request, comms)
    assert len(calls) == 1, "caller-supplied addresses never become arbitrary host-side SSH"


@pytest.mark.parametrize("failure", (FileNotFoundError("tailscale absent"),
                                    subprocess.TimeoutExpired("tailscale", 24)))
def test_a_missing_or_stalled_cli_is_reported_not_an_empty_tailnet(monkeypatch, failure):
    def unavailable(*args, **kwargs):
        raise failure
    monkeypatch.setattr(subprocess, "run", unavailable)
    with pytest.raises(RuntimeError, match="tailscale"):
        TailnetBuses.peers()
