"""How a served MCP endpoint is reachable, which decides who may be given a UI profile.

`tailscale serve status --json` owns which URLs this machine serves and which of them
tailscale publishes to the internet; nothing here keeps a second list of them. Reaching a
tailnet-only URL already proves tailnet membership, the same authority a person carrying a
minted profile from machine to machine was standing in for, so a caller that reached one may
be handed a profile. A funnelled URL proves nothing about its caller: the bearer token is
the only lock, so minting for it stays a deliberate act (`grant-ui`).

The command and the decode are separate, so a test states the argv and decodes a recorded
reply without a tailnet.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar
from urllib.parse import urlparse

from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.transport.probed_command import ProbedCommand


class HttpUrlScheme(DeclaredFamily, affix="Scheme"):
    """An HTTP or HTTPS URL scheme, decoded once at the boundary."""

    default_port: ClassVar[int]
    websocket_scheme: ClassVar[str]

    @classmethod
    def for_url_scheme(cls, scheme: str) -> type[HttpUrlScheme]:
        return cls.member_named(scheme)


class HttpScheme(HttpUrlScheme):
    default_port = 80
    websocket_scheme = "ws"


class HttpsScheme(HttpUrlScheme):
    default_port = 443
    websocket_scheme = "wss"


@dataclass(frozen=True, slots=True)
class ServedTarget:
    """One served URL and the loopback address behind it, as tailscale reports the pair."""

    url: str
    proxy: str

    @classmethod
    def at(cls, name: str, path: str, proxy: str) -> ServedTarget:
        """The URL tailscale serves `name` at: 443 is the bare origin, any other port is in
        the URL, and the root handler adds no path."""
        host, _, port = name.rpartition(":")
        origin = f"https://{host or name}" if port == "443" else f"https://{name}"
        return cls(origin if path == "/" else f"{origin}{path}", proxy)

    def behind(self, port: int) -> bool:
        """Whether this URL proxies to `port` on loopback, which is how a connector
        recognises its own endpoints without being told what they are."""
        return self.proxy.rstrip("/").endswith(f":{port}")


class ServedEndpoint(ABC):
    """One URL this machine serves, and what reaching it proves about its caller."""

    target: ServedTarget

    def profile_offer(self) -> tuple[ProfileOffer, ...]:
        return ()

    @abstractmethod
    def label(self) -> str:
        """How one line names this endpoint to a person."""


@dataclass(frozen=True, slots=True)
class TailnetServed(ServedEndpoint):
    """Served to this tailnet only: tailscale admitted the caller's machine first."""

    target: ServedTarget

    def profile_offer(self) -> tuple[ProfileOffer, ...]:
        return (OfferedByTailnet(self),)

    def label(self) -> str:
        return f"{self.target.url} · tailnet only"


@dataclass(frozen=True, slots=True)
class FunnelServed(ServedEndpoint):
    """Published to the internet by tailscale Funnel: anyone can reach it, so its bearer
    token is the only lock and a profile for it is minted by hand."""

    target: ServedTarget

    def label(self) -> str:
        return f"{self.target.url} · public (Funnel)"


class ProfileOffer(ABC):
    """Whether this machine may hand a UI profile to whoever just asked for one."""

    @abstractmethod
    def refusal(self) -> str:
        """Why not, or nothing when it may."""

    def apply_offer(self, emit: Callable[[ServedEndpoint], int]) -> int | str:
        return self.refusal()


@dataclass(frozen=True, slots=True)
class OfferedByTailnet(ProfileOffer):
    """A tailnet-only endpoint serves the asked-for port, so the caller is on the tailnet."""

    endpoint: ServedEndpoint

    def refusal(self) -> str:
        return ""

    def apply_offer(self, emit: Callable[[ServedEndpoint], int]) -> int | str:
        return emit(self.endpoint)


@dataclass(frozen=True, slots=True)
class NothingTailnetOnly(ProfileOffer):
    """Only public endpoints reach that port, or nothing does: the caller proved nothing by
    reaching it, so a profile for it is minted deliberately (`grant-ui`)."""

    port: int
    served: tuple[ServedEndpoint, ...]

    DEFAULT_SERVE_PORT = 10000
    """Port the suggested `tailscale serve` command uses: not 443/8443 (the usual Funnel
    ports) so the suggestion doesn't collide with existing public serves."""

    def refusal(self) -> str:
        suggest_port = self._free_suggest_port()
        return (f"No tailnet-only endpoint serves 127.0.0.1:{self.port}"
                + (f"; served instead: {', '.join(e.label() for e in self.served)}"
                   if self.served else " and tailscale serves nothing in front of it")
                + f". Serve it to the tailnet (tailscale serve --bg --https={suggest_port}"
                  f" http://127.0.0.1:{self.port}), or mint a profile with grant-ui.")

    def _free_suggest_port(self) -> int:
        """`DEFAULT_SERVE_PORT` when it is free, otherwise the next whole thousand that is.
        Avoids suggesting a port that already hosts a public Funnel entry."""
        taken: set[int] = set()
        for endpoint in self.served:
            parsed = urlparse(endpoint.target.url)
            if parsed.port is not None:
                taken.add(parsed.port)
            else:
                taken.add(HttpUrlScheme.for_url_scheme(parsed.scheme).default_port)
        port = self.DEFAULT_SERVE_PORT
        while port in taken:
            port += 1000
        return port


class ServedEndpoints(ProbedCommand):
    """What this machine serves, asked of tailscale once per call."""

    @classmethod
    def status_command(cls) -> tuple[str, ...]:
        return ("tailscale", "serve", "status", "--json")

    @classmethod
    def listed(cls, status: Mapping[str, Any]) -> tuple[ServedEndpoint, ...]:
        """Every served URL, as the member that says how it is reachable.

        `AllowFunnel` names the host:port combinations tailscale publishes; a served name
        absent from it is reachable from this tailnet alone.
        """
        funnelled = {name for name, allowed in (status.get("AllowFunnel") or {}).items() if allowed}
        return tuple(
            (FunnelServed if name in funnelled else TailnetServed)(
                ServedTarget.at(name, path, (handler or {}).get("Proxy") or ""))
            for name, web in sorted((status.get("Web") or {}).items())
            for path, handler in sorted((web.get("Handlers") or {}).items())
        )

    @classmethod
    def behind(cls, served: Sequence[ServedEndpoint], port: int) -> tuple[ServedEndpoint, ...]:
        """The served URLs in front of `port`, tailnet-only ones first: a connector asks for
        its own port and is given the endpoints that reach it, with the one that can offer a
        profile at the head."""
        return tuple(sorted((endpoint for endpoint in served if endpoint.target.behind(port)),
                            key=lambda endpoint: not endpoint.profile_offer()))

    @classmethod
    def served(cls) -> tuple[ServedEndpoint, ...]:
        """What tailscale serves here, or nothing when it is not running or serves nothing."""
        status, output = cls.run(cls.status_command())
        if status != 0:
            return ()
        try:
            return cls.listed(json.loads(output))
        except ValueError:
            return ()

    @classmethod
    def serving(cls, port: int) -> tuple[ServedEndpoint, ...]:
        return cls.behind(cls.served(), port)

    @classmethod
    def offer(cls, served: Sequence[ServedEndpoint], port: int) -> ProfileOffer:
        """Whether a caller that reached `port` may be handed a profile, as the member that
        says so: the first tailnet-only endpoint in front of it, or everything that is."""
        behind = cls.behind(served, port)
        return next((offer for endpoint in behind
                     for offer in endpoint.profile_offer()), NothingTailnetOnly(port, behind))

    @classmethod
    def offered(cls, port: int) -> ProfileOffer:
        return cls.offer(cls.served(), port)
