"""Private native-UI connection data, independent of the HTTP server and its stores."""

from __future__ import annotations

import http.client
import os
import socket
import re
import ssl
from abc import abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Self

from yarl import URL

from .nominal.field_codec import (
    FieldCodec, Id128, MalformedRecord, NominalSpelling, NominalText, NullMember, PathText,
    RootId, SingleFieldValue, TaggedRecordMember, TaggedUnion, TextRepresentation,
)
from .storage.private_path import PrivateDirectoryRole, PrivateFileRole
from .storage.store_files import ExclusiveDirectoryAtomicFile


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


class EndpointRoute(TaggedUnion, affix="Route"):
    """How this machine reaches a declared endpoint. The endpoint says where a bus is; this
    says how to get there from here, which is the client's fact and not the bus's."""

    @abstractmethod
    def connection(self, endpoint: HttpsMcpEndpoint, context: ssl.SSLContext,
                   timeout: float) -> http.client.HTTPSConnection:
        """A connection to that endpoint by this route."""


@dataclass(frozen=True)
class ResolvedRoute(NullMember, EndpointRoute):
    """Resolve the endpoint's own name, which is what a public endpoint needs and what every
    profile did before an endpoint could be tailnet-only."""

    def connection(self, endpoint: HttpsMcpEndpoint, context: ssl.SSLContext,
                   timeout: float) -> http.client.HTTPSConnection:
        return endpoint.connection(context, timeout)


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
        directory = PrivateDirectoryRole.read(path.parent, refusal)
        try:
            ExclusiveDirectoryAtomicFile(path=path, directory=directory).write_text(
                FieldCodec.compact(FieldCodec.encode(self), allow_nan=False))
        finally:
            os.close(directory)
        return path

    @classmethod
    def read(cls, path: Path) -> Self:
        """Decode once from a private regular file, without following a leaf symlink."""
        descriptor = PrivateFileRole.read(
            path.expanduser(), ValueError("The UI profile is not a private credential file"))
        with os.fdopen(descriptor, "r", encoding="utf-8") as source:
            return FieldCodec.decode(cls, FieldCodec.parse(source.read()))


@dataclass(frozen=True)
class UiGrantSummary:
    """Safe console output: the credential itself exists only in the private profile."""

    grant_id: UiGrantId
    namespace: RootId
    expires_at: float
    profile: Annotated[Path, PathText]
