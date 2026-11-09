"""Asking a peer for a UI profile, decoded from what its CLI says.

No tailnet and no ssh: the argv is stated and the replies are the ones a peer actually
gives — a profile, the offer-ui refusal, an older install with no such verb.
"""

import json
from dataclasses import replace

import pytest
from yarl import URL

from agent_comms.nominal.field_codec import FieldCodec, RootId
from agent_comms.transport.connector_profile import (
    BearerSecret,
    HttpsMcpEndpoint,
    UiConnectionProfile,
    UiCredential,
    UiGrantId,
)
from agent_comms.transport.connector_store import (
    HeldUiProfiles,
    MisdirectedProfile,
    OpenableProfile,
)
from agent_comms.transport.tailnet_buses import PeerHoldsBus, TailnetPeer
from agent_comms.transport.tailnet_profiles import (
    HoldingPeerProfile,
    OfferedPeerProfile,
    PeerAnsweredNothing,
    PeerRefusedProfile,
    TailnetProfiles,
)

GAMING = PeerHoldsBus(TailnetPeer("ts-gaming", "100.119.57.99", True), 43)


def profile_text() -> str:
    profile = UiConnectionProfile(
        HttpsMcpEndpoint(URL("https://ts-gaming.tail553874.ts.net:8443")),
        UiCredential(UiGrantId("7f3a9c2b" * 4), RootId("0" * 32), 1793000000.0,
                     BearerSecret("secret-token")))
    return FieldCodec.compact(FieldCodec.encode(profile), allow_nan=False)


def test_the_ask_runs_the_peers_own_connector_over_ssh():
    command = TailnetProfiles.ask_command(GAMING)
    assert command[0] == "ssh" and "100.119.57.99" in command
    assert command[-1].endswith("agent-comms-connector offer-ui --access ui_access"), (
        f"the peer decides its own root and endpoint, so the ask names neither: {command}")
    assert "BatchMode=yes" in command, "a peer wanting a password is a failure, not a prompt"


def test_a_profile_comes_back_held_under_the_peer(tmp_path, monkeypatch):
    monkeypatch.setattr(HeldUiProfiles, "directory", classmethod(lambda cls: tmp_path))
    offered = TailnetProfiles.asked(GAMING, 0, profile_text() + "\n")
    assert isinstance(offered, OfferedPeerProfile)
    assert not list(tmp_path.iterdir()), "Core returns a profile, not a path on its own host"
    asked = offered.hold()
    assert isinstance(asked, HoldingPeerProfile) and asked.reason() == ""
    assert asked.path == tmp_path / "ts-gaming.json"
    held = UiConnectionProfile.read(asked.path)
    assert str(held.endpoint.url) == "https://ts-gaming.tail553874.ts.net:8443"
    assert held.credential.token == BearerSecret("secret-token")


def test_a_peer_serving_only_a_funnel_says_no_in_its_own_words():
    refusal = ("No tailnet-only endpoint serves 127.0.0.1:18766; served instead: "
               "https://ts-gaming.tail553874.ts.net · public (Funnel). Serve it to the tailnet")
    asked = TailnetProfiles.asked(GAMING, 0, refusal)
    assert isinstance(asked, PeerRefusedProfile)
    assert "would not hand out a profile" in asked.reason()
    assert "public (Funnel)" in asked.reason(), "the peer's own sentence reaches the picker"


@pytest.mark.parametrize("status, output", (
    (255, "ssh: connect to host 100.119.57.99 port 22: Connection refused"),
    (2, "agent-comms-connector: invalid choice: 'offer-ui'"),
    (0, ""),
))
def test_a_channel_that_answered_nothing_is_not_a_refusal(status, output):
    asked = TailnetProfiles.asked(GAMING, status, output)
    assert isinstance(asked, PeerAnsweredNothing)
    assert "gave no profile" in asked.reason()


def test_a_reply_that_is_not_a_profile_at_all_is_named_as_such():
    asked = TailnetProfiles.asked(GAMING, 0, json.dumps({"something": "else"}))
    assert isinstance(asked, PeerAnsweredNothing)
    assert asked.found is GAMING


def test_a_held_profile_pointing_anywhere_but_mcp_is_not_openable(tmp_path, monkeypatch):
    """The 404 Tristan hit: a profile minted for the bare origin, held beside good ones.

    It decodes and its grant is live, so the only thing wrong with it is where it points,
    and the family says so rather than offering a row that posts to / and fails.
    """
    import time

    from agent_comms.transport.connector_profile import MCP_PATH

    monkeypatch.setattr(HeldUiProfiles, "directory", classmethod(lambda cls: tmp_path))
    live = time.time() + 3600
    held = FieldCodec.decode(UiConnectionProfile, FieldCodec.parse(profile_text().encode()))
    unexpired = replace(held, credential=replace(held.credential, expires_at=live))
    at_origin = replace(unexpired,
                        endpoint=HttpsMcpEndpoint(unexpired.endpoint.url.with_path("/")))
    at_mcp = replace(unexpired,
                     endpoint=HttpsMcpEndpoint(unexpired.endpoint.url.with_path(MCP_PATH)))

    misdirected = HeldUiProfiles.entry(at_origin.write_new(tmp_path / "origin.json"), live - 1)
    openable = HeldUiProfiles.entry(at_mcp.write_new(tmp_path / "mcp.json"), live - 1)
    assert isinstance(misdirected, MisdirectedProfile) and not misdirected.openable
    assert "not /mcp" in misdirected.label(), "the row says where it points"
    assert isinstance(openable, OpenableProfile) and openable.openable


def test_asking_again_replaces_what_was_held_and_revokes_it(tmp_path, monkeypatch):
    """Every pick minted another grant, and the old one stayed live with a row beside the
    new one that looked exactly the same."""
    monkeypatch.setattr(HeldUiProfiles, "directory", classmethod(lambda cls: tmp_path))
    asked: list[tuple[str, ...]] = []
    monkeypatch.setattr(TailnetProfiles, "run",
                        classmethod(lambda cls, command: asked.append(tuple(command)) or (0, profile_text())))

    first = TailnetProfiles.ask(GAMING, TailnetProfiles.held_grants(GAMING)).hold()
    held = sorted(path.name for path in tmp_path.iterdir())
    second = TailnetProfiles.ask(GAMING, TailnetProfiles.held_grants(GAMING)).hold()

    assert held == ["ts-gaming.json"] and first.path == second.path
    assert sorted(path.name for path in tmp_path.iterdir()) == ["ts-gaming.json"], (
        "one bus, one held profile, however many times it is asked")
    revokes = [command for command in asked if "revoke-ui" in command[-1]]
    assert len(revokes) == 1, f"the replaced grant is ended on the peer: {asked}"
    assert "7f3a9c2b" in revokes[0][-1], "by the id the replaced profile named"
