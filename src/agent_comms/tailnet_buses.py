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
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar


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


class PeerBus(ABC):
    """What a probe found on one peer.

    Every peer gets a member, including the ones that answered nothing: a peer left out of
    the list is indistinguishable from a peer without a bus, and the two need different
    answers from whoever reads this.
    """

    peer: TailnetPeer
    openable: ClassVar[bool] = False

    @abstractmethod
    def label(self) -> str:
        """How a picker names this peer in one line."""


@dataclass(frozen=True, slots=True)
class PeerHoldsBus(PeerBus):
    """Its CLI answered with its registry, so there is a bus here to open."""

    peer: TailnetPeer
    threads: int
    openable = True

    def label(self) -> str:
        return f"{self.peer.name} · {self.threads} threads"


@dataclass(frozen=True, slots=True)
class PeerWithoutBus(PeerBus):
    """Reachable, and its CLI is not installed or answered nothing we can read."""

    peer: TailnetPeer
    refusal: str

    def label(self) -> str:
        return f"{self.peer.name} · no bus answered"


@dataclass(frozen=True, slots=True)
class PeerUnreachable(PeerBus):
    """Tailscale says it is up, and ssh did not get there."""

    peer: TailnetPeer
    refusal: str

    def label(self) -> str:
        return f"{self.peer.name} · unreachable"


@dataclass(frozen=True, slots=True)
class PeerOffline(PeerBus):
    """Tailscale says it is down, so nothing is asked of it."""

    peer: TailnetPeer

    def label(self) -> str:
        return f"{self.peer.name} · offline"


class TailnetBuses:
    """The probe: one ssh call per online peer, asking its own CLI for its registry."""

    timeout_seconds: ClassVar[float] = 12
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
    def run(cls, command: Sequence[str]) -> tuple[int, str]:
        done = subprocess.run(tuple(command), capture_output=True, text=True, errors="replace",
                              timeout=cls.timeout_seconds * 2, check=False)
        return done.returncode, done.stdout + done.stderr

    @classmethod
    def peers(cls) -> tuple[TailnetPeer, ...]:
        status, output = cls.run(cls.status_command())
        if status != 0:
            return ()
        return TailnetPeer.listed(json.loads(output))

    @classmethod
    def seen(cls) -> tuple[PeerBus, ...]:
        """Every peer this tailnet reports, with what each one answered."""
        found = []
        for peer in cls.peers():
            if not peer.online or not peer.address:
                found.append(cls.found(peer, 0, ""))
                continue
            status, output = cls.run(cls.probe_command(peer))
            found.append(cls.found(peer, status, output))
        return tuple(found)
