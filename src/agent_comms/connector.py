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
import logging
import secrets
import sys
import time
import urllib.request
from abc import abstractmethod
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Annotated, ClassVar, Literal, Self, get_args
from urllib.parse import urlencode

from aiohttp import web
from pydantic import JsonValue

from .cli_commands import DeclaredOptions
from .command import Command
from .cli_protocol import CoreError
from .comms import Comms, client_wire
from .declared_family import DeclaredFamily
from .errors import ToolRefusedError, UnregisteredThreadError
from .field_codec import FieldCodec, MalformedRecord, PathText, option
from .goal_actions import DirectiveGoalAction
from .connector_store import (
    CODE_SECONDS, AuthorizationRequest, ConnectorGrants, ConnectorStore, IssuedCode,
    RegisteredClient, digest,
)
from .messages import Message
from .commander.standing import UnownedWheelSpin
from .todos import DirectiveText
from .tool_caller import ThreadCaller
from .tools import CommsChannelsTool, ReadTool, ToolRequest

LOOPBACK = "127.0.0.1"
PORT = 18766


@dataclass(frozen=True)
class DirectorCaller(ThreadCaller):
    """claude-ai speaks for the owner: each message it sends a thread is a directive, recorded as a
    request when that thread holds owner requests."""

    def judged(self, comms, problem, verdict):
        return verdict.reviewed(comms.goals, problem, comms.registry.require(self.name).incarnation)

    def require_directive(self, comms: Comms, body: str) -> None:
        """A directive passes DirectiveText. claude-ai directs the commanders, so the
        commander rules review does not gate it."""
        DirectiveText.require(body)

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


def declared(record: type, values: Mapping[str, object]) -> dict[str, object]:
    """The values a record declares; an OAuth peer may send parameters it does not read."""
    names = {FieldCodec.wire_key(item) for item in fields(record)}
    return {key: value for key, value in values.items() if key in names}


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

    @classmethod
    def of(cls, tool: type[ToolRequest]) -> ToolCall:
        """A call of a tool the connector exposes, by the name it exposes it under."""
        (name,) = (name for name, exposed in Connector.exposed().items() if exposed is tool)
        return cls(name)


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
            FieldCodec.encode(ToolsCallReply(self.id, await self.result(connector), Path(sys.executable)))
        )

    async def result(self, connector: Connector) -> ToolsCallResult:
        tool = Connector.exposed().get(self.params.name)
        if tool is None:
            return ToolsCallResult.failed(
                ToolRefusedError(f"The connector exposes no tool {self.params.name!r}.")
            )
        try:
            reply = await asyncio.to_thread(connector.invoke, tool, self.params.arguments)
            return ToolsCallResult((TextContent(json.dumps(reply)),), False, reply)
        except Exception as error:
            logging.exception("Connector tool %s failed", self.params.name)
            return ToolsCallResult.failed(error)


@dataclass(frozen=True)
class TextContent:
    text: str
    type: Literal["text"] = "text"


@dataclass(frozen=True)
class ToolsCallResult:
    """A tool's encoded reply, or its failed call encoded as an MCP error result."""

    content: tuple[TextContent, ...]
    is_error: bool = field(metadata={"wire_name": "isError"})
    structured_content: dict[str, JsonValue] = field(
        default_factory=dict,
        metadata={"wire_name": "structuredContent", "wire_omit_default": True},
    )

    @classmethod
    def failed(cls, error: Exception) -> ToolsCallResult:
        return cls((TextContent(CoreError.from_exception(error).model_dump_json()),), True)


@dataclass(frozen=True)
class ToolsCallReply:
    id: int | str
    result: ToolsCallResult
    interpreter: Annotated[Path, PathText]
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

    @contextmanager
    def opened_wire(self) -> Iterator[Comms]:
        """The connector's caller must exist at startup and at each later call."""
        with client_wire(self.root) as comms:
            comms.registry.require(CALLER.name)
            yield comms

    def invoke(
        self, tool: type[ToolRequest], arguments: Mapping[str, object]
    ) -> dict[str, JsonValue]:
        with self.opened_wire() as comms:
            return tool.invoke(comms, arguments, caller=CALLER)

    def application(self) -> web.Application:
        with self.opened_wire():
            pass
        app = web.Application()
        app.cleanup_ctx.append(self.watch_orders)
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

    async def watch_orders(self, app: web.Application) -> AsyncIterator[None]:
        """The running Core service advances stalled orders without a commander call."""
        task = asyncio.create_task(UnownedWheelSpin.watch(self.opened_wire))
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

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
        except (MalformedRecord, json.JSONDecodeError) as error:
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
        except MalformedRecord as error:
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
        except (MalformedRecord, json.JSONDecodeError) as error:
            return web.json_response(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": str(error)}}
            )
        return await message.answer(self)


@dataclass(frozen=True, kw_only=True)
class ConnectorVerb(DeclaredFamily, DeclaredOptions, Command, affix="Verb"):
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
        return cls.member_named(args.command).from_arguments(args)


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


@dataclass(frozen=True, kw_only=True)
class RoundTripVerb(ConnectorVerb):
    """Explicit qualification for a configured connector: its service runs the selected
    runtime and its loopback read agrees with this bus. Native-only installs do not need it."""

    help = "Read the bus through the running connector service and compare"
    root: Annotated[Path, PathText] = option("root", help="The comms root it proves")
    runtime: Annotated[Path, PathText] = option("runtime", help="The Python the service runs")
    port: int = option("--port", default=PORT, help="The service's loopback port")

    def apply(self, ctx: ConnectorStore) -> int | str:
        read = ToolCall.of(CommsChannelsTool)
        call = ToolsCallMessage(jsonrpc="2.0", id=0, params=read)
        with ctx.granted("upgrade round trip") as token:
            request = urllib.request.Request(
                f"http://{LOOPBACK}:{self.port}/mcp",
                data=json.dumps(FieldCodec.encode(call)).encode(),
                headers={"Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(request, timeout=60) as response:
                response = FieldCodec.decode(ToolsCallReply, json.load(response))
        running = response.interpreter
        reply = response.result
        if running != self.runtime:
            return f"The connector service runs {running}, not the installed {self.runtime}"
        if reply.is_error:
            return f"The connector round trip failed: {reply.content[0].text}"
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

        ctx.update(approved)
        return 0


@dataclass(frozen=True, kw_only=True)
class RevokeVerb(ConnectorVerb):
    help = "Revoke every issued token and code"

    def apply(self, ctx: ConnectorStore) -> int | str:
        ctx.update(lambda grants: replace(grants, requests={}, codes={}, access={}, refresh={}))
        return 0


def main(argv: Sequence[str] | None = None) -> int | str:
    return ConnectorVerb.parse(argv).apply(ConnectorStore.owned())
