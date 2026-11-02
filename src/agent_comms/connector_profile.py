"""Private native-UI connection data, independent of the HTTP server and its stores."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

from yarl import URL

from .nominal.field_codec import (
    FieldCodec, Id128, MalformedRecord, NominalSpelling, NominalText, PathText,
    RootId, SingleFieldValue, TextRepresentation,
)
from .private_path import PrivateDirectoryRole, PrivateFileRole
from .store_files import ExclusiveDirectoryAtomicFile


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
    def read(cls, path: Path) -> UiConnectionProfile:
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
