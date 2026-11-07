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

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from agent_comms.connector_profile import UiConnectionProfile
from agent_comms.connector_store import HeldUiProfiles
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
    def hold(cls, found: PeerHoldsBus, profile: UiConnectionProfile) -> Path:
        """Write it where this machine keeps the profiles it holds, under the peer and grant
        it names: the same place and the same name `hold-ui` writes, so one bus has one
        profile however it arrived."""
        directory = HeldUiProfiles.directory()
        return profile.write_new(
            directory / f"{found.peer.name}-{profile.credential.grant_id}.json")

    @classmethod
    def ask(cls, found: PeerHoldsBus) -> AskedPeer:
        """Ask the peer and hold what it gives; blocking, so callers run it off the UI."""
        status, output = cls.run(cls.ask_command(found))
        return cls.asked(found, status, output)
