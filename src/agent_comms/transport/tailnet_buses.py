"""Which machines on this tailnet hold a bus.

Each peer's own CLI owns the answer, so the probe asks it over ssh rather than reading
another machine's files or guessing from a port: the connector serves loopback only, and
ssh is the access this fleet already has. Tailscale owns which machines exist and which
are up; nothing here keeps a second list of them.

The command and the decode are separate, so a test states the argv and decodes a recorded
reply without a tailnet.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, ClassVar

from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.transport.probed_command import ProbedCommand


@dataclass(frozen=True, slots=True)
class TailnetPeer:
    """One machine tailscale reports, by the address this machine reaches it on."""

    name: str
    address: str
    online: bool

    @classmethod
    def listed(cls, status: Mapping[str, Any]) -> tuple[TailnetPeer, ...]:
        """Every peer in `tailscale status --json`, by name.

        Self is excluded: this machine's bus is the local one, which the picker already
        offers, and a peer with no address is kept and named rather than dropped, because
        a machine missing from the list reads as a machine without a bus.
        """
        peers = status.get("Peer") or {}
        return tuple(sorted(
            (cls(peer.get("HostName") or "", cls.reached(peer), bool(peer.get("Online")))
             for peer in peers.values()),
            key=lambda peer: peer.name,
        ))

    @staticmethod
    def reached(peer: Mapping[str, Any]) -> str:
        """The address to use: its tailnet IP, which resolves without MagicDNS."""
        addresses = peer.get("TailscaleIPs") or ()
        return next((address for address in addresses if ":" not in address), "")


class ProbedPeer:
    """A member a tailnet probe can report.

    Every peer the probe sees becomes one of these, which is why "every outcome crosses
    the wire" is answerable by asking the family rather than by a roster someone keeps in
    step. A machine a person typed in is a PeerBus and is not one of these: no probe
    produces it, so it is not a discovery outcome and a test about discovery should not
    have to name it as an exception (MEMB-2).
    """


class PeerBus(DeclaredFamily, ABC, affix="Peer"):
    """What a probe found on one peer.

    Every peer gets a member, including the ones that answered nothing: a peer left out of
    the list is indistinguishable from a peer without a bus, and the two need different
    answers from whoever reads this.
    """

    peer: TailnetPeer

    @abstractmethod
    def label(self) -> str:
        """How a picker names this peer in one line."""

    def on_tailnet(self, peers: tuple[TailnetPeer, ...]) -> TailnetPeer:
        """This row's peer, proven to be one Core's own tailnet reports.

        Core ssh's to whatever this returns, so nothing else may decide it. A row the
        probe itself produced names a peer it already saw; a row a person typed has to be
        matched against the list here, because otherwise a caller - including a remote one
        over the connector - could name any host on Core's network and have Core ssh to
        it. The guard was already in ServedCommsTailnetProfileTool and it is the reason
        this is a resolution rather than a widening.
        """
        if self.peer not in peers:
            # Its own case, in its own words: this row WAS a peer when the probe saw it.
            raise ValueError("This bus is no longer a peer on Core's tailnet; discover again.")
        return self.peer


@dataclass(frozen=True)
class PeerHoldsBus(ProbedPeer, PeerBus):
    """Its CLI answered with its registry, so there is a bus here to open."""

    peer: TailnetPeer
    threads: int

    def label(self) -> str:
        return f"{self.peer.name} · {self.threads} threads"


@dataclass(frozen=True)
class NamedHost(PeerBus):
    """A machine a person typed in, rather than one tailscale reported.

    Tristan: let a bus be added by hand - type an address, save it, and be able to remove
    saved ones again. Nobody has asked this host anything yet, so it claims no thread
    count: a PeerHoldsBus means "its CLI answered with its registry", and building one of
    those for a host nobody has spoken to would put a lie in the data and render it as
    "myhost · 0 threads". It is openable because asking is the whole point, and the host
    says no for itself through its own ProfileOffer.
    """

    peer: TailnetPeer

    def label(self) -> str:
        return self.peer.name

    def on_tailnet(self, peers: tuple[TailnetPeer, ...]) -> TailnetPeer:
        """The peer this typed text names, by Tailscale's own name or address.

        Typing adds a bus Core's tailnet reports and discovery did not offer - a machine
        whose CLI answered nothing, say - not an arbitrary host. The text is matched; it
        never becomes an ssh destination on its own.
        """
        typed = self.peer.name
        for peer in peers:
            if typed in (peer.name, peer.address):
                return peer
        raise ValueError(f"{typed} is not a peer on Core's tailnet; check the name or address.")

    @classmethod
    def typed(cls, address: str) -> NamedHost:
        """What a person typed, taken as both the name and the address to reach it on.

        A typed host has no separate name until its own connector answers with one, and
        inventing one here would be a second place deciding what this machine is called.
        """
        return cls(TailnetPeer(address.strip(), address.strip(), True))


@dataclass(frozen=True)
class PeerWithoutBus(ProbedPeer, PeerBus):
    """Reachable, and its CLI is not installed or answered nothing we can read."""

    peer: TailnetPeer
    refusal: str

    def label(self) -> str:
        return f"{self.peer.name} · no bus answered"


@dataclass(frozen=True)
class PeerUnreachable(ProbedPeer, PeerBus):
    """Tailscale says it is up, and ssh did not get there."""

    peer: TailnetPeer
    refusal: str

    def label(self) -> str:
        return f"{self.peer.name} · unreachable"


@dataclass(frozen=True)
class PeerOffline(ProbedPeer, PeerBus):
    """Tailscale says it is down, so nothing is asked of it."""

    peer: TailnetPeer

    def label(self) -> str:
        return f"{self.peer.name} · offline"


class TailnetBuses(ProbedCommand):
    """The probe: one ssh call per online peer, asking its own CLI for its registry."""

    timeout_seconds: ClassVar[float] = 12
    """Longer than the default: each call crosses the tailnet to another machine."""
    remote_cli: ClassVar[str] = "~/.local/bin/agent-comms"

    @classmethod
    def status_command(cls) -> tuple[str, ...]:
        return ("tailscale", "status", "--json")

    @classmethod
    def probe_command(cls, peer: TailnetPeer) -> tuple[str, ...]:
        """Batch mode so a peer that wants a password is unreachable, not a prompt nobody
        is there to answer."""
        return ("ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={int(cls.timeout_seconds)}",
                "-o", "StrictHostKeyChecking=accept-new", peer.address,
                f"{cls.remote_cli} threads")

    @classmethod
    def found(cls, peer: TailnetPeer, status: int, output: str) -> PeerBus:
        """One peer's reply, decoded into the member that says what it means."""
        if not peer.online or not peer.address:
            return PeerOffline(peer)
        if status != 0:
            return PeerUnreachable(peer, output.strip().splitlines()[-1] if output.strip() else "")
        try:
            threads = json.loads(output)["threads"]
        except (ValueError, KeyError, TypeError) as refusal:
            return PeerWithoutBus(peer, str(refusal))
        return PeerHoldsBus(peer, len(threads))

    @classmethod
    def peers(cls) -> tuple[TailnetPeer, ...]:
        status, output = cls.run(cls.status_command())
        if status != 0:
            raise RuntimeError(output.strip() or "Tailscale status did not answer")
        return TailnetPeer.listed(json.loads(output))

    @classmethod
    def probe(cls, peer: TailnetPeer) -> PeerBus:
        if not peer.online or not peer.address:
            return cls.found(peer, 0, "")
        status, output = cls.run(cls.probe_command(peer))
        return cls.found(peer, status, output)

    @classmethod
    def seen(cls) -> tuple[PeerBus, ...]:
        """Independent peers answer concurrently; one unreachable host cannot queue all others."""
        with ThreadPoolExecutor() as workers:
            return tuple(workers.map(cls.probe, cls.peers()))
