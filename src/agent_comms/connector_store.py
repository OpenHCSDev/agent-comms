"""Connector grant declarations and storage, independent of the HTTP transport."""
from __future__ import annotations

import hashlib
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import ClassVar

from pydantic import JsonValue

from .locked_store import LockedStore, RootFile

ACCESS_SECONDS = 3600
CODE_SECONDS = 300


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


@dataclass(frozen=True)
class RegisteredClient:
    name: str


@dataclass(frozen=True)
class AuthorizationRequest:
    """One authorization waiting for the owner, keyed by its PKCE challenge."""

    client_id: str
    code_challenge: str
    state: str
    approved: bool = False


@dataclass(frozen=True)
class IssuedCode:
    client_id: str
    code_challenge: str
    expires_at: float


@dataclass(frozen=True)
class IssuedAccess:
    client_id: str
    expires_at: float


@dataclass(frozen=True)
class ConnectorGrants:
    """Registered clients and outstanding grants; every secret is stored as its digest."""

    clients: dict[str, RegisteredClient] = field(default_factory=dict)
    requests: dict[str, AuthorizationRequest] = field(default_factory=dict)
    codes: dict[str, IssuedCode] = field(default_factory=dict)
    access: dict[str, IssuedAccess] = field(default_factory=dict)
    refresh: dict[str, str] = field(default_factory=dict)

    def issued(self, client_id: str) -> tuple[ConnectorGrants, dict[str, JsonValue]]:
        """A new access and refresh token pair for the client, dropping expired grants."""
        now = time.time()
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        grants = replace(
            self,
            codes={key: code for key, code in self.codes.items() if code.expires_at > now},
            access={key: held for key, held in self.access.items() if held.expires_at > now}
            | {digest(access): IssuedAccess(client_id, now + ACCESS_SECONDS)},
            refresh=self.refresh | {digest(refresh): client_id},
        )
        return grants, {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": ACCESS_SECONDS,
            "refresh_token": refresh,
        }

    def holds(self, token: str) -> bool:
        held = self.access.get(digest(token))
        return held is not None and held.expires_at > time.time()


@dataclass(frozen=True, slots=True)
class ConnectorStore(RootFile, LockedStore[ConnectorGrants]):
    filename: ClassVar[str] = "grants.json"

    @classmethod
    def owned(cls) -> ConnectorStore:
        directory = Path.home() / ".local/state/agent-comms/connector"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        return cls(cls.path_in(directory))

    record_type = ConnectorGrants

    def empty(self) -> ConnectorGrants:
        return ConnectorGrants()

    @contextmanager
    def granted(self, client_id: str) -> Iterator[str]:
        """An access token for a caller on this machine, revoked when its call is done."""
        token = secrets.token_urlsafe(32)
        held = IssuedAccess(client_id, time.time() + CODE_SECONDS)
        key = digest(token)
        self.update(lambda grants: replace(grants, access=grants.access | {key: held}))
        try:
            yield token
        finally:
            self.update(
                lambda grants: replace(
                    grants, access={k: v for k, v in grants.access.items() if k != key}
                )
            )
