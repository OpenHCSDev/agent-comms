"""Connector grants and browser trust, independent of the HTTP transport."""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import ClassVar, Literal, Self, get_args
from urllib.parse import urlencode

from .field_codec import FieldCodec
from .locked_store import DurableFile, LockedStore

ACCESS_SECONDS = 3600
CODE_SECONDS = 300
ClaudeCallback = Literal["https://claude.ai/api/mcp/auth_callback"]
CALLBACK: str = get_args(ClaudeCallback)[0]


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


@dataclass(frozen=True)
class RegisteredClient:
    name: str


@dataclass(frozen=True, kw_only=True)
class ExpiringGrant:
    expires_at: float

    def live(self, now: float) -> bool:
        return self.expires_at > now

    @classmethod
    def retained(cls, records: dict[str, Self], now: float) -> dict[str, Self]:
        return {key: grant for key, grant in records.items() if grant.live(now)}


@dataclass(frozen=True)
class AuthorizationQuery:
    response_type: Literal["code"]
    client_id: str
    redirect_uri: ClaudeCallback
    code_challenge: str
    code_challenge_method: Literal["S256"]
    state: str = ""

    @property
    def request_id(self) -> str:
        """A repeated query reuses its wait, never another client's PKCE challenge."""
        return digest(json.dumps(FieldCodec.encode(self), sort_keys=True, separators=(",", ":")))

    def redirect(self, code: str) -> str:
        return f"{self.redirect_uri}?{urlencode({'code': code, 'state': self.state})}"


@dataclass(frozen=True)
class AuthorizationRequest(ExpiringGrant):
    """Only a waiting authorization; consuming its secret deletes the request."""

    query: AuthorizationQuery
    approval_digest: str


@dataclass(frozen=True)
class TrustedBrowser:
    """The owner's trust belongs to a browser secret, never a client or request ID."""

    created_at: float
    cookie_name: ClassVar[str] = "__Host-agent-comms-owner"
    cookie_seconds: ClassVar[int] = 365 * 24 * 3600


@dataclass(frozen=True)
class IssuedCode(ExpiringGrant):
    client_id: str
    code_challenge: str


@dataclass(frozen=True)
class IssuedAccess(ExpiringGrant):
    client_id: str


@dataclass(frozen=True)
class IssuedTokens:
    """The OAuth token response; secrets leave storage only in this returned value."""

    access_token: str
    refresh_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int = ACCESS_SECONDS


@dataclass(frozen=True)
class ConnectorGrants:
    """Registered clients and grants; every retained secret is stored as its digest."""

    clients: dict[str, RegisteredClient] = field(default_factory=dict)
    requests: dict[str, AuthorizationRequest] = field(default_factory=dict)
    codes: dict[str, IssuedCode] = field(default_factory=dict)
    access: dict[str, IssuedAccess] = field(default_factory=dict)
    refresh: dict[str, str] = field(default_factory=dict)
    trusted_browsers: dict[str, TrustedBrowser] = field(default_factory=dict)

    def unexpired(self, now: float) -> ConnectorGrants:
        return replace(
            self,
            requests=AuthorizationRequest.retained(self.requests, now),
            codes=IssuedCode.retained(self.codes, now),
            access=IssuedAccess.retained(self.access, now),
        )

    def trusts(self, browser_secret: str) -> bool:
        return digest(browser_secret) in self.trusted_browsers

    def code_issued(self, query: AuthorizationQuery, code: str, now: float) -> ConnectorGrants:
        return replace(
            self,
            requests={key: held for key, held in self.requests.items() if key != query.request_id},
            codes=self.codes | {
                digest(code): IssuedCode(
                    query.client_id, query.code_challenge, expires_at=now + CODE_SECONDS
                )
            },
        )

    def issued(self, client_id: str) -> tuple[ConnectorGrants, IssuedTokens]:
        """A new access and refresh token pair, dropping every expired grant and wait."""
        now = time.time()
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        current = self.unexpired(now)
        grants = replace(
            current,
            access=current.access
            | {digest(access): IssuedAccess(client_id, expires_at=now + ACCESS_SECONDS)},
            refresh=current.refresh | {digest(refresh): client_id},
        )
        return grants, IssuedTokens(access, refresh)

    def holds(self, token: str) -> bool:
        held = self.access.get(digest(token))
        return held is not None and held.live(time.time())


@dataclass(frozen=True, slots=True)
class ConnectorStore(DurableFile, LockedStore[ConnectorGrants]):
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
        held = IssuedAccess(client_id, expires_at=time.time() + CODE_SECONDS)
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
