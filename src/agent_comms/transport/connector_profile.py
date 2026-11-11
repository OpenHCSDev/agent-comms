"""Private native-UI connection data, independent of the HTTP server and its stores."""

from __future__ import annotations

import http.client
import os
import re
import socket
import ssl
from abc import abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Self

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from yarl import URL

from agent_comms.nominal.field_codec import (
    FieldCodec,
    Id128,
    MalformedRecord,
    NominalSpelling,
    NominalText,
    NullMember,
    PathText,
    RootId,
    SingleFieldValue,
    TaggedRecordMember,
    TaggedUnion,
    TextRepresentation,
)
from agent_comms.storage.private_path import PrivateDirectoryRole, PrivateFileRole
from agent_comms.storage.store_files import ExclusiveDirectoryAtomicFile


class UiGrantId(Id128):
    """Public revocation identity; it is not the bearer secret."""


class BearerSecret(NominalText):
    """An opaque HTTP bearer credential, never a command-line argument."""


class BearerSecretText(NominalSpelling[BearerSecret]):
    spelling = re.compile(r"[A-Za-z0-9._~+/-]+=*")


class UrlText(TextRepresentation[URL]):
    @classmethod
    def from_text(cls, value: str) -> URL:
        return URL(value)

    @classmethod
    def encode(cls, value: object) -> str:
        if not isinstance(value, URL):
            raise MalformedRecord("Expected a parsed URL")
        return str(value)


@dataclass(frozen=True)
class HttpsMcpEndpoint(SingleFieldValue):
    """An owner-selected public MCP resource; credentials never enter its URL."""

    url: Annotated[URL, UrlText]

    def __post_init__(self) -> None:
        refusal = "The remote MCP endpoint requires HTTPS and no userinfo, query or fragment."
        with MalformedRecord.refusing(refusal):
            self.url.origin()
            permitted = (
                self.url.with_scheme("https").with_user(None)
                .with_query(None).with_fragment(None)
            )
        if self.url != permitted:
            raise MalformedRecord(refusal)

    def connection(self, context: ssl.SSLContext, timeout: float) -> http.client.HTTPSConnection:
        """A connection to this endpoint's origin, which its validation made absolute."""
        match self.url.host:
            case str() as host:
                return http.client.HTTPSConnection(
                    host, self.url.port, context=context, timeout=timeout)
        raise MalformedRecord(f"The remote MCP endpoint {self.url} names no host.")


class DialedConnection(http.client.HTTPSConnection):
    """Dials one address while verifying the certificate of the name it was given.

    `http.client` resolves the host it verifies, which is the same name: on a tailnet whose
    DNS this machine does not accept, that name resolves to tailscale's public ingress and
    the tailnet-only port there answers nothing (TLS EOF, measured 2026-11-07).
    """

    def __init__(self, host: str, address: str, port: int | None, **options) -> None:
        super().__init__(host, port, **options)
        self.address = address

    def connect(self) -> None:
        raw = socket.create_connection((self.address, self.port), self.timeout,
                                       self.source_address)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


MCP_PATH = "/mcp"
"""Where a connector serves MCP: its own route, the resource its metadata names and the
endpoint a profile points at are one fact. A profile minted for the bare origin posts to /
and is answered 404 (Tristan, 2026-11-07: "i either get 404 or ...")."""


OBSERVE_PATH = "/observe"
"""Where a connector serves the observation socket. A remote UI derives its own URL from the
endpoint and this, so the two spellings cannot drift apart."""


class PinnedResolver(AbstractResolver):
    """Answers every lookup with one address, keeping the asked-for name for TLS.

    aiohttp resolves the URL's host itself, so a client session reaching a tailnet-only
    endpoint by name lands on tailscale's public ingress exactly as `http.client` did
    (Tristan, 2026-11-07: "ClientConnectorError: ts-gaming.tail553874.t..."). The returned
    hostname is what verifies the certificate; the host is what the socket connects to.
    """

    def __init__(self, address: str) -> None:
        self.address = address

    async def resolve(self, host: str, port: int = 0,
                      family: socket.AddressFamily = socket.AF_INET) -> list[ResolveResult]:
        return [ResolveResult(hostname=host, host=self.address, port=port,
                              family=family, proto=0, flags=0)]

    async def close(self) -> None:
        """Nothing is held open to close."""


class EndpointRoute(TaggedUnion, affix="Route"):
    """How this machine reaches a declared endpoint. The endpoint says where a bus is; this
    says how to get there from here, which is the client's fact and not the bus's.

    Both transports ask: the calls go over `http.client` and the observation socket over
    aiohttp, and a route that only taught one of them left the observation resolving a name
    that goes somewhere else.
    """

    @abstractmethod
    def connection(self, endpoint: HttpsMcpEndpoint, context: ssl.SSLContext,
                   timeout: float) -> http.client.HTTPSConnection:
        """A connection to that endpoint by this route."""

    @abstractmethod
    def socket_session(self) -> aiohttp.ClientSession:
        """The session an observation socket opens on, reaching the endpoint by this route."""

    def reaches(self, address: str) -> bool:
        """Whether this route goes to that address, so a UI can tell that a profile it holds
        and a machine the tailnet reports are one bus rather than two rows."""
        return False


@dataclass(frozen=True)
class ResolvedRoute(NullMember, EndpointRoute):
    """Resolve the endpoint's own name, which is what a public endpoint needs and what every
    profile did before an endpoint could be tailnet-only."""

    def connection(self, endpoint: HttpsMcpEndpoint, context: ssl.SSLContext,
                   timeout: float) -> http.client.HTTPSConnection:
        return endpoint.connection(context, timeout)

    def socket_session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession()


@dataclass(frozen=True)
class ReachedRoute(TaggedRecordMember, EndpointRoute):
    """Dial this address and verify the endpoint's name against its certificate: the address
    is how the asking machine reached the peer, recorded by the machine that knows it."""

    address: str

    def connection(self, endpoint: HttpsMcpEndpoint, context: ssl.SSLContext,
                   timeout: float) -> http.client.HTTPSConnection:
        match endpoint.url.host:
            case str() as host:
                return DialedConnection(host, self.address, endpoint.url.port,
                                        context=context, timeout=timeout)
        raise MalformedRecord(f"The remote MCP endpoint {endpoint.url} names no host.")

    def socket_session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(resolver=PinnedResolver(self.address)))

    def reaches(self, address: str) -> bool:
        return self.address == address


@dataclass(frozen=True)
class UiCredential:
    """The one returned secret and immutable issuance facts, not client-side authority."""

    grant_id: UiGrantId
    namespace: RootId
    expires_at: float
    token: BearerSecret = field(repr=False)


@dataclass(frozen=True)
class UiConnectionProfile:
    """Local UI configuration. The server still decides expiry, scope and revocation."""

    endpoint: HttpsMcpEndpoint
    credential: UiCredential
    route: EndpointRoute = ResolvedRoute()
    """How this machine reaches that endpoint; resolving its name unless told otherwise."""

    def connection(self, context: ssl.SSLContext, timeout: float) -> http.client.HTTPSConnection:
        """A connection to this profile's bus, by the route it was held with."""
        return self.route.connection(self.endpoint, context, timeout)

    def write_new(self, path: Path) -> Path:
        """Publish privately and atomically; never replace an existing credential."""
        path = path.expanduser()
        path = path.parent.resolve() / path.name
        PrivateDirectoryRole.make_parents(path.parent)
        refusal = ValueError("The UI profile requires an owner-only private directory")
        with PrivateDirectoryRole.reading(path.parent, refusal) as directory:
            ExclusiveDirectoryAtomicFile(path=path, directory=directory).write_text(
                FieldCodec.compact(FieldCodec.encode(self), allow_nan=False))
        return path

    @classmethod
    def read(cls, path: Path) -> Self:
        """Decode once from a private regular file, without following a leaf symlink."""
        with PrivateFileRole.stream(
            path.expanduser(), ValueError("The UI profile is not a private credential file"),
        ) as source:
            return FieldCodec.decode(cls, FieldCodec.parse(source.read().decode("utf-8")))


@dataclass(frozen=True)
class UiGrantSummary:
    """Safe console output: the credential itself exists only in the private profile."""

    grant_id: UiGrantId
    namespace: RootId
    expires_at: float
    profile: Annotated[Path, PathText]
