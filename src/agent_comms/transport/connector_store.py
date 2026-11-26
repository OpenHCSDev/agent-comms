"""Connector grants and browser trust, independent of the HTTP transport."""

from __future__ import annotations

import hashlib
import ipaddress
import re
import secrets
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Annotated, ClassVar, Literal, Self

from metaclass_registry.mro_dispatch import MroDispatch, handles
from yarl import URL

from agent_comms.nominal.declared_family import DeclaredFamily, UnknownFamilyMember
from agent_comms.nominal.field_codec import (
    FieldCodec,
    MalformedRecord,
    NominalSpelling,
    NominalText,
    RootId,
    SingleFieldValue,
    boundary_decoder,
)
from agent_comms.storage.locked_store import DocumentChange, ExternalFile, FilesRequest
from agent_comms.storage.maintenance_barrier import CarriedDocument
from agent_comms.storage.store_files import DependentStateTier, DrainedWriters, SingleFileStore
from agent_comms.storage.typed_table import GuardedExternalStore
from agent_comms.transport.connector_caller import (
    CALLER,
    ConnectorCaller,
    ForkingCaller,
    GrantRefusedError,
    ReadOnlyConnectorCaller,
    ScopedUiCaller,
    UiCaller,
    UiReadCaller,
)
from agent_comms.transport.connector_profile import (
    MCP_PATH,
    BearerSecret,
    HttpsMcpEndpoint,
    UiConnectionProfile,
    UiCredential,
    UiGrantId,
    UiGrantSummary,
    UrlText,
)
from agent_comms.transport.held_ui_profiles import connector_state_directory
from agent_comms.transport.served_endpoints import HttpScheme, HttpsScheme, HttpUrlScheme

ACCESS_SECONDS = 3600


UI_GRANT_SECONDS = 7 * 24 * 3600
"""A UI grant is minted by the host owner (grant-ui on the host) and revocable by its ID, so it
may outlive an OAuth access token. UiGrantAccess declares its own default lifetime."""


CODE_SECONDS = 300


class OAuthRedirectAuthority(DeclaredFamily, affix="RedirectAuthority"):
    """The protocol authority that validates and compares one callback URL."""

    refusal: ClassVar[str] = (
        "An OAuth redirect URI requires HTTPS or an HTTP loopback host, with no "
        "userinfo, query, or fragment."
    )

    @classmethod
    @boundary_decoder
    def of(cls, url: URL) -> OAuthRedirectAuthority:
        """Decode external URL shape once; concrete members own protocol-specific identity."""

        if not url.is_absolute():
            raise MalformedRecord(cls.refusal)
        if url.raw_user is not None or url.raw_password is not None:
            raise MalformedRecord(cls.refusal)
        if url.query or url.fragment:
            raise MalformedRecord(cls.refusal)
        try:
            scheme = HttpUrlScheme.for_url_scheme(url.scheme)
        except UnknownFamilyMember as error:
            raise MalformedRecord(cls.refusal) from error
        return _OAuthSchemeAuthority.handler_for(scheme)(_OAUTH_SCHEME_AUTHORITY, scheme(), url)

    @abstractmethod
    def matches(self, requested: URL, registered: URL) -> bool:
        """Whether the requested callback is the registered callback under this protocol."""


@dataclass(frozen=True)
class HttpsRedirectAuthority(OAuthRedirectAuthority):
    def matches(self, requested: URL, registered: URL) -> bool:
        return requested == registered


@dataclass(frozen=True)
class LoopbackRedirectAuthority(OAuthRedirectAuthority):
    @classmethod
    @boundary_decoder
    def for_url(cls, url: URL) -> LoopbackRedirectAuthority:
        host = url.host
        if host is None:
            raise MalformedRecord(cls.refusal)
        if host == "localhost":
            return cls()
        try:
            address = ipaddress.ip_address(host)
        except ValueError as error:
            raise MalformedRecord(cls.refusal) from error
        if not address.is_loopback:
            raise MalformedRecord(cls.refusal)
        return cls()

    def matches(self, requested: URL, registered: URL) -> bool:
        """A native client may choose its port; the raw remaining URL stays exact."""

        return requested.with_port(None) == registered.with_port(None)


class _OAuthSchemeAuthority(MroDispatch):
    @handles(HttpsScheme)
    def https(self, scheme: HttpsScheme, url: URL) -> OAuthRedirectAuthority:
        return HttpsRedirectAuthority()

    @handles(HttpScheme)
    def http(self, scheme: HttpScheme, url: URL) -> OAuthRedirectAuthority:
        return LoopbackRedirectAuthority.for_url(url)


_OAUTH_SCHEME_AUTHORITY = _OAuthSchemeAuthority()


@dataclass(frozen=True)
class OAuthRedirectUri(SingleFieldValue):
    """A registered OAuth callback: HTTPS, or HTTP on a native loopback listener."""

    url: Annotated[URL, UrlText]

    def __post_init__(self) -> None:
        OAuthRedirectAuthority.of(self.url)

    def matches_registration(self, registered: OAuthRedirectUri) -> bool:
        return OAuthRedirectAuthority.of(registered.url).matches(self.url, registered.url)


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class OAuthClientId(NominalText):
    """The opaque identifier of one registered OAuth client."""


class OAuthClientIdText(NominalSpelling[OAuthClientId]):
    spelling = re.compile(r"[\w-]+")


class OAuthClientAuthentication(DeclaredFamily, affix="OAuthClient"):
    """The registered token-endpoint authentication contract."""

    @abstractmethod
    def accepts(self, secret: str) -> bool: ...


@dataclass(frozen=True)
class PublicOAuthClient(OAuthClientAuthentication):
    def accepts(self, secret: str) -> bool:
        return not secret


@dataclass(frozen=True)
class ConfidentialOAuthClient(OAuthClientAuthentication):
    secret_digest: str

    def accepts(self, secret: str) -> bool:
        return secrets.compare_digest(self.secret_digest, digest(secret))


class ApprovalRefused(ValueError):
    """A one-time approval is missing, expired or already consumed."""

    def __init__(self) -> None:
        super().__init__("This approval link is unknown, expired or used.")


class ApprovalQueueFull(ValueError):
    """Unknown browsers have filled the bounded waiting queue."""


@dataclass(frozen=True)
class RegisteredClient:
    name: str
    redirect_uris: tuple[OAuthRedirectUri, ...]
    authentication: OAuthClientAuthentication

    def accepts_redirect(self, redirect_uri: OAuthRedirectUri) -> bool:
        return any(redirect_uri.matches_registration(registered) for registered in self.redirect_uris)

    def accepts_secret(self, secret: str) -> bool:
        return self.authentication.accepts(secret)


class GrantLifetime:
    """Permanent grants and expiring grants share the same retention question."""

    def live(self, now: float) -> bool:
        return True

    @classmethod
    def retained(cls, records: dict[str, Self], now: float) -> dict[str, Self]:
        return {key: grant for key, grant in records.items() if grant.live(now)}


@dataclass(frozen=True, kw_only=True)
class ExpiresAt:
    """Good until a wall-clock time: live while that time is still ahead."""

    expires_at: float

    def live(self, now: float) -> bool:
        return self.expires_at > now


@dataclass(frozen=True, kw_only=True)
class ExpiringGrant(ExpiresAt, GrantLifetime):
    pass


@dataclass(frozen=True)
class AuthorizationQuery:
    response_type: Literal["code"]
    client_id: OAuthClientId
    redirect_uri: OAuthRedirectUri
    code_challenge: str
    code_challenge_method: Literal["S256"]
    state: str = ""

    @property
    def request_id(self) -> str:
        """A repeated query reuses its wait, never another client's PKCE challenge."""
        return FieldCodec.digest(self)

    def redirect(self, code: str, issuer: str) -> str:
        return str(self.redirect_uri.url.with_query({
            "code": code, "state": self.state, "iss": issuer,
        }))


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
    client_id: OAuthClientId
    code_challenge: str
    redirect_uri: OAuthRedirectUri

    def redeemed_by(
        self, client_id: OAuthClientId, challenge: str, redirect_uri: OAuthRedirectUri, now: float,
    ) -> bool:
        return (
            self.live(now)
            and (self.client_id, self.code_challenge) == (client_id, challenge)
            and self.redirect_uri == redirect_uri
        )


class ConnectorAccess(GrantLifetime, DeclaredFamily, affix=""):
    """One bearer digest resolves to one declared authority, never a store-name policy."""

    def caller(self) -> ConnectorCaller:
        if not self.live(time.time()):
            raise GrantRefusedError("Unknown or expired connector grant.")
        return self.authorized_caller()

    def authorized_caller(self) -> ConnectorCaller:
        raise GrantRefusedError("Unknown or expired connector grant.")

    def holds_thread(self, thread: str) -> bool:
        return False

    def holds_ui_grant(self, grant_id: UiGrantId) -> bool:
        return False

    def reconnects(self, thread: str) -> bool:
        """Whether this is the standing reconnect URL of ``thread``'s inbox watch."""
        return False

    def lapses_with(self, thread: str) -> bool:
        """Whether revoking or re-granting ``thread`` ends this: its grant, and its watch's
        reconnect URL, which would otherwise keep reading a revoked thread's inbox."""
        return self.holds_thread(thread)

    def reopened[T](self, reconnect: Callable[[str], T], refused: T) -> T:
        """What an inbox watch URL with this digest opens: only a watch's reconnect does."""
        return refused

    def spent_in(self, grants: ConnectorGrants, key: str) -> ConnectorGrants:
        """The grants once a watch URL with this digest is redeemed: only a watch's reconnect
        is spent by it, so a watch path never revokes an MCP grant, and an armed ticket's
        redemption returns the same document, which the store does not rewrite."""
        return grants


@dataclass(frozen=True)
class UnissuedAccess(ConnectorAccess):
    """An absent bearer has no caller authority."""


class ExpiringDirectorAccess(ExpiringGrant, ConnectorAccess, declared_name=None):
    def authorized_caller(self) -> ConnectorCaller:
        return CALLER


class AccessLabel(NominalText):
    """The recorded label of a bearer with an unattributed producer."""


class AccessLabelText(NominalSpelling[AccessLabel]):
    # Retain every spelling recorded in the old document, including an empty label.
    spelling = re.compile(r"[\s\S]*")


@dataclass(frozen=True)
class ExistingDirectorAccess(ExpiringDirectorAccess):
    """A retained Director bearer whose OAuth/service producer was not recorded."""

    label: AccessLabel


@dataclass(frozen=True)
class IssuedAccess(ExpiringDirectorAccess):
    """An access token issued from the registered OAuth client's code or refresh."""

    client_id: OAuthClientId


@dataclass(frozen=True)
class TemporaryServiceAccess(ExpiringGrant, ConnectorAccess, declared_name=None):
    """A local proof's purpose and lifetime, independent of its caller permissions."""

    purpose: str


@dataclass(frozen=True)
class ServiceAccess(TemporaryServiceAccess, ExpiringDirectorAccess):
    """A temporary Director grant used by owner-operated service consumers."""


@dataclass(frozen=True)
class ReadServiceAccess(TemporaryServiceAccess):
    """A local service read needs no registered colleague or write permission."""

    def authorized_caller(self) -> ConnectorCaller:
        return ReadOnlyConnectorCaller()


@dataclass(frozen=True)
class ThreadGrant(ConnectorAccess):
    thread: str

    def authorized_caller(self) -> ConnectorCaller:
        return ForkingCaller(self.thread)

    def holds_thread(self, thread: str) -> bool:
        return self.thread == thread


@dataclass(frozen=True)
class WatchReconnect(ConnectorAccess):
    """An inbox watch's standing reconnect URL, kept as its digest beside the grants because
    the connector issued it and must honour it across its own restart: every install restarts
    the connector, and a reset client re-dials into the new process (2026-11-06 03:15, three
    watches refused across one install). It names no MCP caller and holds no thread grant."""

    thread: str

    def reconnects(self, thread: str) -> bool:
        return self.thread == thread

    def lapses_with(self, thread: str) -> bool:
        return self.reconnects(thread)

    def reopened[T](self, reconnect: Callable[[str], T], refused: T) -> T:
        return reconnect(self.thread)

    def spent_in(self, grants: ConnectorGrants, key: str) -> ConnectorGrants:
        return replace(grants, access={held: grant for held, grant in grants.access.items()
                                       if held != key})


@dataclass(frozen=True)
class UiGrantAccess(ExpiringGrant, ConnectorAccess, declared_name=None):
    """Shared UI issuance facts; the concrete member declares its caller capability."""

    maximum_seconds: ClassVar[int] = UI_GRANT_SECONDS
    default_seconds: ClassVar[int] = 3600
    """A UI grant's own default lifetime, an hour; not the OAuth access clock, which may move."""
    caller_type: ClassVar[type[ScopedUiCaller]]

    grant_id: UiGrantId
    namespace: RootId

    def authorized_caller(self) -> ConnectorCaller:
        return self.caller_type(self.namespace)

    def holds_ui_grant(self, grant_id: UiGrantId) -> bool:
        return self.grant_id == grant_id

    def credential(self, token: BearerSecret) -> UiCredential:
        return UiCredential(self.grant_id, self.namespace, self.expires_at, token)


@dataclass(frozen=True)
class UiReadAccess(UiGrantAccess):
    caller_type = UiReadCaller


@dataclass(frozen=True)
class UiAccess(UiGrantAccess):
    caller_type = UiCaller


@dataclass(frozen=True)
class IssuedTokens:
    """The OAuth token response; secrets leave storage only in this returned value."""

    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = ACCESS_SECONDS


@dataclass(frozen=True)
class ConnectorGrants:
    """Registered clients and grants; every retained secret is stored as its digest."""

    clients: dict[str, RegisteredClient] = field(default_factory=dict)
    requests: dict[str, AuthorizationRequest] = field(default_factory=dict)
    codes: dict[str, IssuedCode] = field(default_factory=dict)
    access: dict[str, ConnectorAccess] = field(default_factory=dict)
    refresh: dict[str, str] = field(default_factory=dict)
    trusted_browsers: dict[str, TrustedBrowser] = field(default_factory=dict)
    waiting_limit: ClassVar[int] = 8

    def waiting(
        self, query: AuthorizationQuery, approval_digest: str, now: float
    ) -> ConnectorGrants:
        """Issue the secret this call minted, whether or not the request is already waiting.

        Keeping an older record instead would leave the browser holding a secret whose digest
        this store never kept, so `require_approval` could only ever refuse it: one fact, the
        live secret for a request, split across the caller and the store with nothing joining
        them. Re-issuing replaces the whole record, so the newest link is the only live one.
        """
        current = self.unexpired(now)
        if query.request_id not in current.requests and len(current.requests) >= self.waiting_limit:
            raise ApprovalQueueFull("The owner approval queue is full; try again later.")
        pending = AuthorizationRequest(query, approval_digest, expires_at=now + CODE_SECONDS)
        return replace(current, requests=current.requests | {query.request_id: pending})

    def require_approval(self, request_id: str, secret: str, now: float) -> AuthorizationRequest:
        if request_id not in self.requests:
            raise ApprovalRefused()
        held = self.requests[request_id]
        if not held.live(now) or not secrets.compare_digest(held.approval_digest, digest(secret)):
            raise ApprovalRefused()
        return held

    def unexpired(self, now: float) -> ConnectorGrants:
        return replace(
            self,
            requests=AuthorizationRequest.retained(self.requests, now),
            codes=IssuedCode.retained(self.codes, now),
            access=ConnectorAccess.retained(self.access, now),
        )

    def trusts(self, browser_secret: str) -> bool:
        return digest(browser_secret) in self.trusted_browsers

    def code_issued(self, query: AuthorizationQuery, code: str, now: float) -> ConnectorGrants:
        return replace(
            self,
            requests={key: held for key, held in self.requests.items() if key != query.request_id},
            codes=self.codes | {
                digest(code): IssuedCode(
                    query.client_id, query.code_challenge, query.redirect_uri,
                    expires_at=now + CODE_SECONDS,
                )
            },
        )

    def issued(self, client_id: OAuthClientId) -> DocumentChange[ConnectorGrants, IssuedTokens]:
        """A new access and refresh token pair, dropping every expired grant and wait."""
        now = time.time()
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        current = self.unexpired(now)
        grants = replace(
            current,
            access=current.access
            | {digest(access): IssuedAccess(client_id, expires_at=now + ACCESS_SECONDS)},
            refresh=current.refresh | {digest(refresh): str(client_id)},
        )
        return DocumentChange(grants, IssuedTokens(access, refresh))

    def caller_for(self, token: str) -> ConnectorCaller:
        return self.access.get(digest(token), UnissuedAccess()).caller()

    def thread_held(self, thread: str) -> bool:
        return any(grant.holds_thread(thread) for grant in self.access.values())

    def access_without(self, thread: str) -> dict[str, ConnectorAccess]:
        return {key: grant for key, grant in self.access.items() if not grant.lapses_with(thread)}

    def ui_issued(
        self, namespace: RootId, seconds: int,
        grant_type: type[UiGrantAccess] = UiReadAccess,
    ) -> DocumentChange[ConnectorGrants, UiCredential]:
        if not 0 < seconds <= grant_type.maximum_seconds:
            raise ValueError(
                f"A UI grant lasts between one and {grant_type.maximum_seconds} seconds")
        now = time.time()
        token = BearerSecret(secrets.token_urlsafe(32))
        held = grant_type(UiGrantId.mint(), namespace, expires_at=now + seconds)
        current = self.unexpired(now)
        return DocumentChange(
            replace(current, access=current.access | {digest(token): held}),
            held.credential(token),
        )

    def ui_revoked(self, grant_id: UiGrantId) -> ConnectorGrants:
        return replace(self, access={
            key: grant for key, grant in self.access.items()
            if not grant.holds_ui_grant(grant_id)
        })


class ConnectorFile(ExternalFile):
    """The credential filename shared by its external declaration and selected reader."""

    filename = "grants.json"


class ConnectorCredentialsFile(DrainedWriters, GuardedExternalStore, ConnectorFile):
    """The canonical external credential resource retained by the installer.

    Its declaration names the global file; ConnectorStore instances read a selected
    live or retained file, rather than lending the carry their live location.
    """

    @classmethod
    def directory(cls) -> Path:
        """Where this machine keeps its connector state, grants and held profiles alike."""
        return connector_state_directory()

    @classmethod
    def path(cls) -> Path:
        return cls.path_in(cls.directory())

    @classmethod
    def practice_opening(cls, files: FilesRequest, directory: Path) -> None:
        for resource in cls.resources(files):
            path = files.location(resource)
            if path.is_file():
                ConnectorStore(path).read()


@dataclass(frozen=True, slots=True)
class ConnectorStore(SingleFileStore, CarriedDocument[ConnectorGrants], ConnectorFile,
                     DependentStateTier):
    @classmethod
    def owned(cls) -> ConnectorStore:
        path = ConnectorCredentialsFile.path()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return cls(path)

    record_type = ConnectorGrants

    def empty(self) -> ConnectorGrants:
        return ConnectorGrants()

    def caller_for(self, token: str) -> ConnectorCaller:
        """Resolve one bearer without deep-copying the grants document on every HTTP request."""
        return self._read_shared().caller_for(token)

    def thread_granted(self, thread: str) -> str:
        token = secrets.token_urlsafe(32)
        return self.update(lambda grants: DocumentChange(
            replace(grants, access=grants.access_without(thread) | {digest(token): ThreadGrant(thread)}),
            token,
        ))

    def watch_reconnect_issued(self, thread: str) -> BearerSecret:
        """A thread's one standing reconnect URL: minting it ends any older one of the thread."""
        token = BearerSecret(secrets.token_urlsafe(32))
        self.update(lambda grants: DocumentChange.to(replace(grants, access={
            key: held for key, held in grants.access.items() if not held.reconnects(thread)
        } | {digest(token): WatchReconnect(thread)})))
        return token

    def watch_reconnect_taken(self, token: BearerSecret) -> ConnectorAccess:
        """What the watch URL's digest held, spent in the same write when it is a reconnect."""
        key = digest(token)

        def taken(grants: ConnectorGrants) -> DocumentChange[ConnectorGrants, ConnectorAccess]:
            held = grants.access.get(key, UnissuedAccess())
            return DocumentChange(held.spent_in(grants, key), held)
        return self.update(taken)

    def thread_revoked(self, thread: str) -> None:
        self.update(lambda grants: DocumentChange.to(replace(grants, access=grants.access_without(thread))))

    def offer_ui_profile(
        self, namespace: RootId, endpoint: HttpsMcpEndpoint,
        seconds: int = UiGrantAccess.default_seconds,
        grant_type: type[UiGrantAccess] = UiReadAccess,
    ) -> UiConnectionProfile:
        """Mint a profile for a caller that already proved its reach, with no file to write.

        `publish_ui_profile` writes the profile before committing the grant, so a failed
        write never activates a bearer. There is nothing to fail here: the file belongs on
        the machine that will hold it, and the caller receives the profile as this verb's
        answer. A grant whose answer never arrives expires on its own and `revoke-ui` ends
        it sooner; that is the whole cost of handing one out, and it is why only a
        tailnet-only endpoint may (served_endpoints.ProfileOffer).
        """
        def issue(grants: ConnectorGrants) -> DocumentChange[ConnectorGrants, UiConnectionProfile]:
            changed = grants.ui_issued(namespace, seconds, grant_type)
            return DocumentChange(changed.document, UiConnectionProfile(endpoint, changed.result))
        return self.update(issue)

    def publish_ui_profile(
        self, namespace: RootId, endpoint: HttpsMcpEndpoint, path: Path,
        seconds: int = UiGrantAccess.default_seconds,
        grant_type: type[UiGrantAccess] = UiReadAccess,
    ) -> UiGrantSummary:
        """Publish the private profile before activating its bearer, in one owned edit.

        DocumentTransactions applies this transition once under the current writer
        cut. A failed profile publication never commits a grant. If the later store
        commit fails, its existing atomic rollback owns recovery. The profile itself
        never grants authority; only the committed server record does.
        """
        def publish(grants: ConnectorGrants) -> DocumentChange[ConnectorGrants, UiGrantSummary]:
            changed = grants.ui_issued(namespace, seconds, grant_type)
            credential = changed.result
            profile = UiConnectionProfile(endpoint, credential).write_new(path)
            return DocumentChange(changed.document, UiGrantSummary(
                credential.grant_id, namespace, credential.expires_at, profile))
        return self.update(publish)

    def revoke_ui(self, grant_id: UiGrantId) -> None:
        self.update(lambda grants: DocumentChange.to(grants.ui_revoked(grant_id)))

    @contextmanager
    def granted(
        self, purpose: str, grant_type: type[TemporaryServiceAccess] = ServiceAccess,
    ) -> Iterator[str]:
        """A local service token, revoked when its proof is done; purpose is not a client ID."""
        token = secrets.token_urlsafe(32)
        held = grant_type(purpose, expires_at=time.time() + CODE_SECONDS)
        key = digest(token)
        self.update(lambda grants: DocumentChange.to(replace(grants, access=grants.access | {key: held})))
        try:
            yield token
        finally:
            self.update(
                lambda grants: DocumentChange.to(replace(
                    grants, access={k: v for k, v in grants.access.items() if k != key}
                ))
            )
