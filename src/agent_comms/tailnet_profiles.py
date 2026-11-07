"""Asking a peer on this tailnet for a UI profile, and holding what it answers.

Tristan: "it's a bus on my private tailnet why would want to have to jupm throuhg hoops".
The hoops were a person minting a profile on one machine and carrying it to another. Both
ends of that are already answered: the peer decides whether its endpoint is tailnet-only
(`served_endpoints.ProfileOffer`), and ssh is the channel this fleet already has to ask it
(`tailnet_buses`). So a picker asks, and holds the answer.

The ask and the decode are separate, so a test states the argv and decodes a recorded
reply without a tailnet.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from pathlib import Path

from agent_comms.connector_profile import ReachedRoute, UiConnectionProfile, UiGrantId
from agent_comms.connector_store import (
    ExpiredProfile, HeldUiProfiles, OpenableProfile,
)
from agent_comms.nominal.field_codec import FieldCodec, MalformedRecord
from agent_comms.tailnet_buses import PeerHoldsBus, TailnetBuses


class AskedPeer(ABC):
    """What asking a peer for a UI profile came back with.

    Every outcome is a member, including the ones that answered nothing: a picker shows the
    reason to the person looking at the row, and "it refused" and "it would not answer" need
    different words.
    """

    found: PeerHoldsBus

    @abstractmethod
    def reason(self) -> str:
        """Why this peer cannot be opened yet, or nothing when it can."""


@dataclass(frozen=True, slots=True)
class HoldingPeerProfile(AskedPeer):
    """It handed over a profile, now held here: this bus can be opened."""

    found: PeerHoldsBus
    path: Path

    def reason(self) -> str:
        return ""


@dataclass(frozen=True, slots=True)
class PeerRefusedProfile(AskedPeer):
    """Its CLI answered, and said no: its own words say what to do about it, which is
    usually serving the endpoint to the tailnet or minting by hand."""

    found: PeerHoldsBus
    refusal: str

    def reason(self) -> str:
        return f"{self.found.peer.name} would not hand out a profile: {self.refusal}"


@dataclass(frozen=True, slots=True)
class PeerAnsweredNothing(AskedPeer):
    """ssh got nowhere, or what came back was not a profile: an older install on the peer
    has no offer-ui, and a password prompt nobody can answer is a failure here too."""

    found: PeerHoldsBus
    refusal: str

    def reason(self) -> str:
        return f"{self.found.peer.name} gave no profile: {self.refusal}"


class TailnetProfiles(TailnetBuses):
    """The ask: one ssh call to a peer's own connector, which decides for itself."""

    connector_cli: str = f"{TailnetBuses.remote_cli}-connector"

    @classmethod
    def ask_command(cls, found: PeerHoldsBus) -> tuple[str, ...]:
        """Its own connector decides: this says nothing about the peer's root or endpoint,
        because the machine being asked owns both."""
        return (*cls.probe_command(found.peer)[:-1], f"{cls.connector_cli} offer-ui")

    @classmethod
    def asked(cls, found: PeerHoldsBus, status: int, output: str) -> AskedPeer:
        """One peer's reply, decoded into the member that says what it means.

        A refusal is the verb's own sentence on a zero status (a `CliResult` refusal is an
        answer, not a crash), so a non-zero status or an undecodable reply is the channel
        failing rather than the peer saying no.
        """
        text = output.strip()
        if status != 0:
            return PeerAnsweredNothing(found, text.splitlines()[-1] if text else "")
        try:
            profile = FieldCodec.decode(UiConnectionProfile, FieldCodec.parse(text.encode()))
        except (MalformedRecord, ValueError) as refusal:
            return (PeerRefusedProfile(found, text) if text and not text.startswith("{")
                    else PeerAnsweredNothing(found, str(refusal)))
        return HoldingPeerProfile(found, cls.hold(found, profile))

    @classmethod
    def revoke_command(cls, found: PeerHoldsBus, grant: UiGrantId) -> tuple[str, ...]:
        """Tell the peer to end a grant this machine no longer holds."""
        return (*cls.probe_command(found.peer)[:-1],
                f"{cls.connector_cli} revoke-ui --grant {grant}")

    @classmethod
    def replaced(cls, found: PeerHoldsBus, path: Path, now: float) -> None:
        """End whatever this machine held for that peer before asking again.

        Without this every pick minted another grant and left the last one live until it
        expired, with a row in the picker that looked identical to the new one. A revoke
        that does not land is not worth refusing the new profile over: the grant it names
        expires on its own, and the person asked for a bus, not for housekeeping.
        """
        match HeldUiProfiles.entry(path, now):
            case OpenableProfile(profile=held) | ExpiredProfile(profile=held):
                cls.run(cls.revoke_command(found, held.credential.grant_id))
            case _:
                pass
        path.unlink(missing_ok=True)

    @classmethod
    def hold(cls, found: PeerHoldsBus, profile: UiConnectionProfile) -> Path:
        """Write it where this machine keeps the profiles it holds, one file per peer: this
        machine asked, so it owns which profile it keeps for that bus, and asking again
        replaces the last one rather than stacking another row beside it.

        With the address this machine reached that peer on, because it is the only one that
        knows it: a tailnet-only endpoint's name resolves to tailscale's public ingress on a
        machine that does not accept tailnet DNS, and nothing answers the tailnet port
        there (measured 2026-11-07, this laptop: TLS EOF by name, 200 by address).
        """
        path = HeldUiProfiles.directory() / f"{found.peer.name}.json"
        cls.replaced(found, path, time.time())
        reached = replace(profile, route=ReachedRoute(found.peer.address))
        return reached.write_new(path)

    @classmethod
    def ask(cls, found: PeerHoldsBus) -> AskedPeer:
        """Ask the peer and hold what it gives; blocking, so callers run it off the UI."""
        status, output = cls.run(cls.ask_command(found))
        return cls.asked(found, status, output)
