"""Which machines on this tailnet hold a bus, decoded from recorded replies.

No tailnet and no ssh here: the probe's command and its decode are separate declarations,
so these state the argv and hand the decode what a peer really answered.
"""

from __future__ import annotations

import json

from agent_comms.transport.tailnet_buses import (
    PeerHoldsBus,
    PeerOffline,
    PeerUnreachable,
    PeerWithoutBus,
    TailnetBuses,
    TailnetPeer,
)

STATUS = {
    "Self": {"HostName": "laptop", "TailscaleIPs": ["100.98.83.32"], "Online": True},
    "Peer": {
        "nodekey:b": {"HostName": "desktop", "TailscaleIPs": ["100.124.133.120", "fd7a::1"],
                      "Online": True},
        "nodekey:a": {"HostName": "Pixel 8a", "TailscaleIPs": ["100.112.213.110"], "Online": True},
        "nodekey:c": {"HostName": "spare", "TailscaleIPs": ["100.1.1.1"], "Online": False},
    },
}


def test_the_peers_are_tailscales_own_list_without_this_machine():
    """Tailscale owns which machines exist; nothing here keeps a second list of them."""
    peers = TailnetPeer.listed(STATUS)
    assert [peer.name for peer in peers] == ["Pixel 8a", "desktop", "spare"]
    assert [peer.online for peer in peers] == [True, True, False]
    # The tailnet IPv4, which resolves without MagicDNS.
    assert [peer.address for peer in peers] == ["100.112.213.110", "100.124.133.120", "100.1.1.1"]
    assert "laptop" not in [peer.name for peer in peers], "this machine's bus is the local one"


def test_the_probe_asks_the_peers_own_cli_in_batch_mode():
    """A peer that wants a password is unreachable, not a prompt nobody is there to answer."""
    peer = TailnetPeer("desktop", "100.124.133.120", True)
    command = TailnetBuses.probe_command(peer)
    assert command[0] == "ssh"
    assert "BatchMode=yes" in command
    assert command[-2:] == ("100.124.133.120", f"{TailnetBuses.remote_cli} threads")
    assert TailnetBuses.status_command() == ("tailscale", "status", "--json")


def test_every_peer_gets_the_member_that_says_what_it_answered():
    """A peer left out of the list reads as a peer without a bus; those need different answers."""
    peer = TailnetPeer("desktop", "100.124.133.120", True)
    registry = json.dumps({"threads": [{"name": "claude-a-toad"}, {"name": "claude-b"}]})

    held = TailnetBuses.found(peer, 0, registry)
    assert isinstance(held, PeerHoldsBus) and held.openable
    assert held.threads == 2 and held.label() == "desktop · 2 threads"

    refused = TailnetBuses.found(peer, 255, "ssh: connect to host: Network is unreachable\n")
    assert isinstance(refused, PeerUnreachable) and not refused.openable
    assert refused.refusal.endswith("Network is unreachable")
    assert refused.label() == "desktop · unreachable"

    silent = TailnetBuses.found(peer, 0, "bash: agent-comms: command not found\n")
    assert isinstance(silent, PeerWithoutBus) and not silent.openable
    assert silent.label() == "desktop · no bus answered"

    down = TailnetBuses.found(TailnetPeer("spare", "100.1.1.1", False), 0, "")
    assert isinstance(down, PeerOffline) and not down.openable
    assert down.label() == "spare · offline"


def test_a_peer_with_no_address_is_named_rather_than_dropped():
    assert isinstance(TailnetBuses.found(TailnetPeer("ghost", "", True), 0, ""), PeerOffline)
    assert TailnetPeer.reached({"TailscaleIPs": ["fd7a::1"]}) == "", "no IPv4 to reach it on"
