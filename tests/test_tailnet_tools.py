"""The same typed operations execute on Core, regardless of the terminal's OS."""

import subprocess

import pytest

from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.transport.connector_caller import UiCaller, UiReadCaller
from agent_comms.transport.core_access import LocalCoreAccess
from agent_comms.transport.tailnet_buses import (
    PeerBus,
    PeerHoldsBus,
    PeerOffline,
    PeerUnreachable,
    PeerWithoutBus,
    ProbedPeer,
    TailnetBuses,
    TailnetPeer,
)
from agent_comms.transport.tailnet_profiles import AskedPeer, PeerRefusedProfile, TailnetProfiles
from agent_comms.transport.tool_request import ServedRequest
from agent_comms.transport.tools.tailnet import (
    CommsTailnetBusesTool,
    CommsTailnetProfileTool,
    TailnetBusesRead,
)
from agent_comms_hooks import ToolRefusedError
from bus_record_fixture import prepare_native_comms

PEER = TailnetPeer("a-discovered-peer", "100.64.1.2", True)
BUS = PeerHoldsBus(PEER, 7)


def test_every_discovery_outcome_crosses_the_existing_wire():
    # Membership, including future members, belongs to PeerBus, not a union at the RPC.
    outcomes = (BUS, PeerOffline(PEER), PeerUnreachable(PEER, "SSH refused"),
                PeerWithoutBus(PEER, "CLI absent"))
    # Every member a probe can REPORT, asked of the family. A machine a person typed in
    # is a PeerBus and not a discovery outcome, so it is not named here as an exception.
    assert {type(entry) for entry in outcomes} == set(PeerBus.members_with(ProbedPeer))
    result = TailnetBusesRead(outcomes)
    assert FieldCodec.decode(TailnetBusesRead, FieldCodec.encode(result)) == result


async def test_the_selected_core_performs_discovery(local_core, monkeypatch):
    monkeypatch.setattr(TailnetBuses, "seen", classmethod(lambda cls: (BUS,)))
    assert await local_core.call(CommsTailnetBusesTool()) == TailnetBusesRead((BUS,))


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


def test_ssh_diagnostics_do_not_become_part_of_a_successful_json_answer(monkeypatch):
    import json
    answer = json.dumps({"threads": [{"name": "one"}]})
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args[0], 0, answer, "Warning: Permanently added host to known hosts.\n"))
    assert TailnetBuses.probe(PEER) == PeerHoldsBus(PEER, 1)


def test_a_typed_address_must_name_a_peer_core_already_reports():
    """Typing adds a bus Core's tailnet reports and discovery did not offer, never an
    arbitrary ssh destination.

    The guard this holds was already in ServedCommsTailnetProfileTool - "a remote caller
    cannot turn a discovered row into arbitrary host-side SSH" - and adding a typed row is
    exactly what it exists to stop, so each member says which peer it means among Core's
    own and one guard covers both kinds.
    """
    from agent_comms.transport.tailnet_buses import NamedHost

    peers = (PEER,)
    # Either spelling Tailscale itself uses resolves to the peer, not to the text.
    assert NamedHost.typed(PEER.name).on_tailnet(peers) == PEER
    assert NamedHost.typed(PEER.address).on_tailnet(peers) == PEER
    assert NamedHost.typed(f"  {PEER.name} ").on_tailnet(peers) == PEER

    for elsewhere in ("evil.example", "10.0.0.1", "localhost", ""):
        with pytest.raises(ValueError, match="not a peer on Core's tailnet"):
            NamedHost.typed(elsewhere).on_tailnet(peers)

    # A discovered row is checked the same way, so a stale one cannot be replayed - and
    # it says its own case, because that row WAS a peer when the probe saw it.
    assert BUS.on_tailnet(peers) == PEER
    with pytest.raises(ValueError, match="no longer a peer on Core's tailnet"):
        PeerHoldsBus(TailnetPeer("ghost", "1.2.3.4", True), 1).on_tailnet(peers)
