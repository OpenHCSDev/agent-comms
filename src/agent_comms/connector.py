"""agent-comms-connector: the claude.ai connector, a Streamable HTTP MCP server beside Core.

The server is a transport over the ToolRequest family: ``tools/list`` reports every member
and whether it changes the bus; ``tools/call`` runs ``invoke()`` as the bus thread claude-ai.
OAuth stands in front of every MCP request. The owner approves each authorization on this
machine with ``agent-comms-connector approve REQUEST``, and tokens are kept only as digests
under ``~/.local/state/agent-comms/connector/``.

Usage:
  agent-comms-connector serve [--root ROOT] [--port PORT] [--issuer URL]
  agent-comms-connector round_trip ROOT RUNTIME [--port PORT]
  agent-comms-connector pending
  agent-comms-connector approve REQUEST
  agent-comms-connector revoke
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import html
import json
import os
import secrets
import socket
import time
import urllib.request
from abc import abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Annotated, ClassVar, Literal, Self, get_args
from urllib.parse import urlencode

from aiohttp import web
from pydantic import JsonValue

from .cli_commands import DeclaredOptions, option
from .cli_protocol import CoreError
from .comms import Comms, client_wire
from .coordination_errors import CoordinationError
from .declared_family import DeclaredFamily
from .errors import UnregisteredThreadError
from .field_codec import FieldCodec, MalformedRecord, PathText
from .goal_actions import DirectiveGoalAction
from .locked_store import LockedStore, RootFile
from .messages import Message
from .standing_rules import RulesDue
from .todos import DirectiveText
from .tool_caller import ThreadCaller
from .tools import CommsChannelsTool, ReadTool, ToolRequest

LOOPBACK = "127.0.0.1"
PORT = 18766


@dataclass(frozen=True)
class DirectorCaller(ThreadCaller):
    """claude-ai speaks for the owner: each message it sends a thread is a directive, recorded as a
    request when that thread holds owner requests."""

    def require_directive(self, comms: Comms, body: str) -> None:
        """A directive passes DirectiveText and needs this caller's rules review, drawn,
        answered and under 20 minutes old."""
        due = tuple(rule.line() for rule in RulesDue.violations(comms, self.name))
        if refused := (*DirectiveText.refused(body), *due):
            raise ValueError("Directive refused, nothing sent: " + " ".join(refused))

    def sent(self, comms: Comms, message: Message) -> None:
        if message.target in comms.registry:  # a channel holds no requests
            comms.goals.update_goal(
                message.target,
                DirectiveGoalAction(text=message.body, seq=message.seq, at=message.timestamp),
            )


CALLER = DirectorCaller("claude-ai")  # Owner-typed calls act as this registered bus thread.
ClaudeCallback = Literal["https://claude.ai/api/mcp/auth_callback"]
CALLBACK: str = get_args(ClaudeCallback)[0]
PROTOCOL_VERSION = "2025-06-18"
CODE_SECONDS = 300
ACCESS_SECONDS = 3600


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def declared(record: type, values: Mapping[str, object]) -> dict[str, object]:
    """The values a record declares; an OAuth peer may send parameters it does not read."""
    names = {FieldCodec.wire_key(item) for item in fields(record)}
    return {key: value for key, value in values.items() if key in names}


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
    lock_rank = 11
    filename: ClassVar[str] = "grants.json"

    @classmethod
    def owned(cls) -> ConnectorStore:
        directory = Path.home() / ".local/state/agent-comms/connector"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        return cls(cls.path_in(directory))

    @property
    def record_type(self) -> type[ConnectorGrants]:
        return ConnectorGrants

    def empty(self) -> ConnectorGrants:
        return ConnectorGrants()

    @contextmanager
    def admitted(self, client_id: str) -> Iterator[str]:
        """An access token for a caller on this machine, revoked when its call is done."""
        token = secrets.token_urlsafe(32)
        held = IssuedAccess(client_id, time.time() + CODE_SECONDS)
        self.update(lambda grants: replace(grants, access=grants.access | {digest(token): held}))
        try:
            yield token
        finally:
            self.update(
                lambda grants: replace(
                    grants, access={k: v for k, v in grants.access.items() if v is not held}
                )
            )


class OAuthError(Exception):
    """An OAuth request the connector refuses, with its RFC 6749 error code."""

    def __init__(self, error: str, description: str) -> None:
        super().__init__(description)
        self.error = error

    def response(self) -> web.Response:
        return web.json_response({"error": self.error, "error_description": str(self)}, status=400)


@dataclass(frozen=True)
class ClientRegistration:
    """Dynamic client registration accepts exactly claude.ai's redirect."""

    redirect_uris: tuple[ClaudeCallback]
    client_name: str = "unnamed client"


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
        return digest(self.code_challenge)[:16]


@dataclass(frozen=True, kw_only=True)
class TokenGrant(DeclaredFamily, affix="Grant"):
    family_discriminator: ClassVar[str] = "grant_type"
    client_id: str
    resource: str = ""

    @classmethod
    def received(cls, form: Mapping[str, object]) -> Self:
        return FieldCodec.decode(cls, dict(form))

    @abstractmethod
    def redeemed(self, grants: ConnectorGrants) -> ConnectorGrants:
        """The grants with this credential spent; refuses a credential it does not hold."""

    def exchange(self, grants: ConnectorGrants) -> tuple[ConnectorGrants, dict[str, JsonValue]]:
        return self.redeemed(grants).issued(self.client_id)


@dataclass(frozen=True, kw_only=True)
class AuthorizationCodeGrant(TokenGrant):
    code: str
    code_verifier: str
    redirect_uri: ClaudeCallback

    def redeemed(self, grants: ConnectorGrants) -> ConnectorGrants:
        issued = grants.codes.get(digest(self.code))
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(self.code_verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        if (
            issued is None
            or issued.expires_at < time.time()
            or (issued.client_id, issued.code_challenge) != (self.client_id, challenge)
        ):
            raise OAuthError("invalid_grant", "The code is unknown, expired or not this client's.")
        return replace(grants, codes={k: v for k, v in grants.codes.items() if v is not issued})


@dataclass(frozen=True, kw_only=True)
class RefreshTokenGrant(TokenGrant):
    refresh_token: str

    def redeemed(self, grants: ConnectorGrants) -> ConnectorGrants:
        refresh = dict(grants.refresh)
        if refresh.pop(digest(self.refresh_token), None) != self.client_id:
            raise OAuthError(
                "invalid_grant", "The refresh token is unknown, revoked or not this client's."
            )
        return replace(grants, refresh=refresh)


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, object] = field(default_factory=dict)
    meta: dict[str, object] = field(default_factory=dict, metadata={"wire_name": "_meta"})


@dataclass(frozen=True, kw_only=True)
class McpMessage(DeclaredFamily, affix="Message"):
    """A JSON-RPC message from the client; each method answers for itself."""

    family_discriminator: ClassVar[str] = "method"
    jsonrpc: Literal["2.0"]

    @classmethod
    def received(cls, data: object) -> Self:
        return FieldCodec.decode(cls, data)

    @abstractmethod
    async def answer(self, connector: Connector) -> web.Response: ...


@dataclass(frozen=True, kw_only=True)
class McpNotification(McpMessage, declared_name=None):
    """A notification gets no reply."""

    params: dict[str, object] = field(default_factory=dict)

    async def answer(self, connector: Connector) -> web.Response:
        return web.Response(status=202)


@dataclass(frozen=True, kw_only=True)
class InitializedMessage(McpNotification, declared_name="notifications/initialized"):
    pass


@dataclass(frozen=True, kw_only=True)
class CancelledMessage(McpNotification, declared_name="notifications/cancelled"):
    pass


@dataclass(frozen=True, kw_only=True)
class McpRequest(McpMessage):
    id: int | str

    async def answer(self, connector: Connector) -> web.Response:
        return web.json_response(
            {"jsonrpc": "2.0", "id": self.id, "result": await self.result(connector)}
        )

    @abstractmethod
    async def result(self, connector: Connector) -> dict[str, JsonValue]: ...


@dataclass(frozen=True, kw_only=True)
class InitializeMessage(McpRequest, declared_name="initialize"):
    params: dict[str, object]

    async def result(self, connector: Connector) -> dict[str, JsonValue]:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "agent-comms", "version": "1"},
        }


@dataclass(frozen=True, kw_only=True)
class PingMessage(McpRequest, declared_name="ping"):
    params: dict[str, object] = field(default_factory=dict)

    async def result(self, connector: Connector) -> dict[str, JsonValue]:
        return {}


@dataclass(frozen=True, kw_only=True)
class ToolsListMessage(McpRequest, declared_name="tools/list"):
    params: dict[str, object] = field(default_factory=dict)

    async def result(self, connector: Connector) -> dict[str, JsonValue]:
        return {
            "tools": [
                {
                    "name": tool.declared_name,
                    "title": tool.label,
                    "description": f"{tool.description} {tool.bus_effect}",
                    "inputSchema": FieldCodec.record_schema(tool),
                    "annotations": {"readOnlyHint": issubclass(tool, ReadTool)},
                }
                for tool in Connector.exposed().values()
            ]
        }


@dataclass(frozen=True, kw_only=True)
class ToolsCallMessage(McpMessage, declared_name="tools/call"):
    id: int | str
    params: ToolCall

    async def answer(self, connector: Connector) -> web.Response:
        return web.json_response(
            FieldCodec.encode(ToolsCallReply(self.id, await self.result(connector)))
        )

    async def result(self, connector: Connector) -> ToolsCallResult:
        tool = Connector.exposed().get(self.params.name)
        if tool is None:
            return ToolsCallResult.refused(
                ValueError(f"The connector exposes no tool {self.params.name!r}.")
            )
        try:
            reply = await asyncio.to_thread(connector.invoke, tool, self.params.arguments)
        except (ValueError, OSError, CoordinationError) as error:
            return ToolsCallResult.refused(error)
        return ToolsCallResult((TextContent(json.dumps(reply)),), False, reply)


@dataclass(frozen=True)
class TextContent:
    text: str
    type: Literal["text"] = "text"


@dataclass(frozen=True)
class ToolsCallResult:
    """A tool's encoded reply, or the refusal standing in its place."""

    content: tuple[TextContent, ...]
    is_error: bool = field(metadata={"wire_name": "isError"})
    structured_content: dict[str, JsonValue] = field(
        default_factory=dict,
        metadata={"wire_name": "structuredContent", "wire_omit_default": True},
    )

    @classmethod
    def refused(cls, error: BaseException) -> ToolsCallResult:
        return cls((TextContent(CoreError.from_exception(error).model_dump_json()),), True)


@dataclass(frozen=True)
class ToolsCallReply:
    id: int | str
    result: ToolsCallResult
    jsonrpc: Literal["2.0"] = "2.0"


@dataclass(frozen=True)
class Connector:
    """The MCP endpoint and the OAuth authorization server in front of it."""

    store: ConnectorStore
    issuer: str
    root: Path | None = None

    @property
    def resource_metadata_url(self) -> str:
        return f"{self.issuer}/.well-known/oauth-protected-resource"

    @staticmethod
    def exposed() -> dict[str, type[ToolRequest]]:
        return {tool.declared_name: tool for tool in ToolRequest.members_with(ToolRequest)}

    def invoke(
        self, tool: type[ToolRequest], arguments: Mapping[str, object]
    ) -> dict[str, JsonValue]:
        with client_wire(self.root) as comms:
            return tool.invoke(comms, arguments, caller=CALLER)

    def application(self) -> web.Application:
        with client_wire(self.root) as comms:
            comms.registry.require(CALLER.name)
        app = web.Application()
        app.add_routes(
            [
                web.get("/.well-known/oauth-protected-resource", self.resource_metadata),
                web.get("/.well-known/oauth-protected-resource/mcp", self.resource_metadata),
                web.get("/.well-known/oauth-authorization-server", self.server_metadata),
                web.post("/register", self.register),
                web.get("/authorize", self.authorize),
                web.post("/token", self.token),
                web.post("/mcp", self.mcp),
            ]
        )
        return app

    async def resource_metadata(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "resource": f"{self.issuer}/mcp",
                "authorization_servers": [self.issuer],
                "bearer_methods_supported": ["header"],
            }
        )

    async def server_metadata(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "issuer": self.issuer,
                "authorization_endpoint": f"{self.issuer}/authorize",
                "token_endpoint": f"{self.issuer}/token",
                "registration_endpoint": f"{self.issuer}/register",
                "response_types_supported": ["code"],
                "grant_types_supported": list(TokenGrant.names()),
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
            }
        )

    async def register(self, request: web.Request) -> web.Response:
        try:
            registration = FieldCodec.decode(
                ClientRegistration, declared(ClientRegistration, await request.json())
            )
        except (MalformedRecord, ValueError) as error:
            return OAuthError("invalid_redirect_uri", str(error)).response()
        client_id = secrets.token_urlsafe(16)
        self.store.update(
            lambda grants: replace(
                grants,
                clients=grants.clients | {client_id: RegisteredClient(registration.client_name)},
            )
        )
        return web.json_response(
            {
                "client_id": client_id,
                "client_id_issued_at": int(time.time()),
                "client_name": registration.client_name,
                "redirect_uris": [CALLBACK],
                "grant_types": list(TokenGrant.names()),
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
            status=201,
        )

    async def authorize(self, request: web.Request) -> web.Response:
        """Hold the authorization until the owner approves it on this machine, then redirect."""
        try:
            query = FieldCodec.decode(
                AuthorizationQuery, declared(AuthorizationQuery, request.query)
            )
        except MalformedRecord as error:
            raise web.HTTPBadRequest(text=str(error)) from error
        code = secrets.token_urlsafe(32)

        def decided(grants: ConnectorGrants) -> ConnectorGrants:
            if query.client_id not in grants.clients:
                raise web.HTTPBadRequest(text="Unknown client.")
            held = grants.requests.get(query.request_id)
            if held is None or not held.approved:
                pending = AuthorizationRequest(query.client_id, query.code_challenge, query.state)
                return replace(
                    grants, requests=grants.requests | {query.request_id: held or pending}
                )
            return replace(
                grants,
                requests={k: v for k, v in grants.requests.items() if v is not held},
                codes=grants.codes
                | {
                    digest(code): IssuedCode(
                        query.client_id, query.code_challenge, time.time() + CODE_SECONDS
                    )
                },
            )

        grants = self.store.update(decided)
        if query.request_id in grants.requests:
            return web.Response(
                content_type="text/html",
                text=(
                    "<!doctype html><meta http-equiv=refresh content=3>"
                    "<title>Approve connector</title>"
                    "<p>Approve this connection on the machine running agent-comms:</p>"
                    f"<pre>agent-comms-connector approve {html.escape(query.request_id)}</pre>"
                    "<p>This page continues once it is approved.</p>"
                ),
            )
        raise web.HTTPFound(f"{CALLBACK}?{urlencode({'code': code, 'state': query.state})}")

    async def token(self, request: web.Request) -> web.Response:
        try:
            grant = TokenGrant.received(await request.post())
        except (MalformedRecord, ValueError) as error:
            return OAuthError("invalid_request", str(error)).response()
        exchanged: list[dict[str, JsonValue]] = []

        def spent(grants: ConnectorGrants) -> ConnectorGrants:
            grants, response = grant.exchange(grants)
            exchanged.append(response)
            return grants

        try:
            self.store.update(spent)
        except OAuthError as refusal:
            return refusal.response()
        return web.json_response(exchanged[0], headers={"Cache-Control": "no-store"})

    async def mcp(self, request: web.Request) -> web.Response:
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not self.store.read().holds(token):
            raise web.HTTPUnauthorized(
                headers={
                    "WWW-Authenticate": (f'Bearer resource_metadata="{self.resource_metadata_url}"')
                }
            )
        try:
            message = McpMessage.received(await request.json())
        except (MalformedRecord, ValueError) as error:
            return web.json_response(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": str(error)}}
            )
        return await message.answer(self)


@dataclass(frozen=True, kw_only=True)
class ConnectorVerb(DeclaredFamily, DeclaredOptions, affix="Verb"):
    """One connector command: its options are its fields, and its parser comes from them."""

    @abstractmethod
    def apply(self, ctx: ConnectorStore) -> int | str: ...

    @classmethod
    def parse(cls, argv: Sequence[str] | None) -> ConnectorVerb:
        parser = argparse.ArgumentParser(
            prog="agent-comms-connector",
            description=__doc__,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        verbs = parser.add_subparsers(dest="command", required=True)
        for member in cls.members_with(cls):
            member.add_parser(verbs)
        args = parser.parse_args(argv)
        return cls.member_named(args.command).from_namespace(args)


@dataclass(frozen=True, kw_only=True)
class ServeVerb(ConnectorVerb):
    help = f"Serve MCP on {LOOPBACK}:{PORT}/mcp"
    root: Annotated[Path, PathText] | None = option(
        "--root", default=None, help="Comms root (default: the active route)"
    )
    port: int = option("--port", default=PORT, help="The loopback port it listens on")
    issuer: str = option(
        "--issuer",
        default=f"http://{LOOPBACK}:{PORT}",
        help="Public origin the OAuth metadata names",
    )

    def apply(self, ctx: ConnectorStore) -> int | str:
        connector = Connector(ctx, self.issuer.rstrip("/"), self.root)
        try:
            app = connector.application()
        except UnregisteredThreadError:
            return f"First run agent-comms register --name {CALLER.name} --worktree DIR"
        web.run_app(app, host=LOOPBACK, port=self.port, access_log=None)
        return 0


@dataclass(frozen=True)
class ServiceProcess:
    """The process listening on the connector's loopback port."""

    pid: int

    @classmethod
    def listening(cls, port: int) -> ServiceProcess:
        local = f"{bytes(reversed(socket.inet_aton(LOOPBACK))).hex().upper()}:{port:04X}"
        sockets = {
            f"socket:[{row[9]}]"
            for row in map(str.split, Path("/proc/net/tcp").read_text().splitlines()[1:])
            if (row[1], row[3]) == (local, "0A")  # 0A: TCP_LISTEN
        }
        for descriptor in Path("/proc").glob("[0-9]*/fd/*") if sockets else ():
            with suppress(OSError):
                if os.readlink(descriptor) in sockets:
                    return cls(int(descriptor.parts[2]))
        raise ValueError(f"No process listens on {LOOPBACK}:{port}")

    @property
    def executable(self) -> Path:
        """The interpreter the service was started as; its venv link is not resolved."""
        return Path(os.fsdecode(Path(f"/proc/{self.pid}/cmdline").read_bytes().split(b"\0")[0]))


@dataclass(frozen=True, kw_only=True)
class RoundTripVerb(ConnectorVerb):
    """What an upgrade proves last: the running connector service runs the installed runtime,
    and a read through its loopback endpoint answers as the same read on this bus."""

    help = "Read the bus through the running connector service and compare"
    root: Annotated[Path, PathText] = option("root", help="The comms root it proves")
    runtime: Annotated[Path, PathText] = option("runtime", help="The Python the service runs")
    port: int = option("--port", default=PORT, help="The service's loopback port")

    def apply(self, ctx: ConnectorStore) -> int | str:
        read = ToolCall(CommsChannelsTool.declared_name)
        call = ToolsCallMessage(jsonrpc="2.0", id=0, params=read)
        try:
            running = ServiceProcess.listening(self.port).executable
            with ctx.admitted("upgrade round trip") as token:
                request = urllib.request.Request(
                    f"http://{LOOPBACK}:{self.port}/mcp",
                    data=json.dumps(FieldCodec.encode(call)).encode(),
                    headers={"Authorization": f"Bearer {token}"},
                )
                with urllib.request.urlopen(request, timeout=60) as response:
                    reply = FieldCodec.decode(ToolsCallReply, json.load(response)).result
        except (OSError, ValueError) as error:
            return f"The connector round trip was refused: {error}"
        if running != self.runtime:
            return f"The connector service runs {running}, not the installed {self.runtime}"
        if reply.is_error:
            return f"The connector round trip was refused: {reply.content[0].text}"
        with client_wire(self.root) as comms:
            if reply.structured_content != CommsChannelsTool.invoke(comms, {}, caller=CALLER):
                return f"The service on port {self.port} reads another bus than {self.root}"
        print(json.dumps({"round_trip": f"{call.params.name} read alike through {running}"}))
        return 0


@dataclass(frozen=True, kw_only=True)
class PendingVerb(ConnectorVerb):
    help = "List authorizations waiting for approval"

    def apply(self, ctx: ConnectorStore) -> int | str:
        grants = ctx.read()
        print(
            json.dumps(
                {
                    key: {"client": grants.clients[held.client_id].name}
                    for key, held in grants.requests.items()
                }
            )
        )
        return 0


@dataclass(frozen=True, kw_only=True)
class ApproveVerb(ConnectorVerb):
    help = "Approve one waiting authorization"
    request: str = option("request", help="The id the authorize page shows")

    def apply(self, ctx: ConnectorStore) -> int | str:
        def approved(grants: ConnectorGrants) -> ConnectorGrants:
            held = grants.requests[self.request]
            return replace(
                grants, requests=grants.requests | {self.request: replace(held, approved=True)}
            )

        try:
            ctx.update(approved)
        except KeyError:
            return f"No authorization {self.request!r} is waiting."
        return 0


@dataclass(frozen=True, kw_only=True)
class RevokeVerb(ConnectorVerb):
    help = "Revoke every issued token and code"

    def apply(self, ctx: ConnectorStore) -> int | str:
        ctx.update(lambda grants: replace(grants, requests={}, codes={}, access={}, refresh={}))
        return 0


def main(argv: Sequence[str] | None = None) -> int | str:
    return ConnectorVerb.parse(argv).apply(ConnectorStore.owned())
