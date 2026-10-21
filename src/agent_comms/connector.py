"""agent-comms-connector: the claude.ai connector, a Streamable HTTP MCP server beside Core.

The server is a transport over the ToolRequest family: ``tools/list`` reports every member
and whether it changes the bus; ``tools/call`` runs ``invoke()`` as the bus thread claude-ai.
OAuth stands in front of every MCP request. An unknown browser waits for the owner's
single-use approval link sent on comms. Its page requires a POST confirmation and then
remembers that browser on this issuer;
subsequent sign-ins need no repeated step. Grant storage retains only secret digests under
``~/.local/state/agent-comms/connector/``.

Usage:
  agent-comms-connector serve --root ROOT [--port PORT] [--issuer URL]
  agent-comms-connector round_trip ROOT [--port PORT]
  agent-comms-connector install-service --root ROOT [--port PORT] [--issuer URL]
  agent-comms-connector revoke
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import logging
import secrets
import signal
import sys
import time
from abc import abstractmethod
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Annotated, ClassVar, Literal, Self

from aiohttp import web
from aiohttp.typedefs import Handler
from aiohttp.web_urldispatcher import AbstractResource, ResourceRoute, SystemRoute
from pydantic import JsonValue


from .cli_bridges.shared import DeclaredVerb
from .cli_protocol import CoreError
from .command import Command
from .comms import Comms, client_wire
from .declared_family import DeclaredFamily, UnknownFamilyMember
from .errors import RelationViolationError, ToolRefusedError, UnregisteredThreadError
from .coordination_errors import CoordinationError
from .field_codec import (
    Argument, FieldCodec, GitCommitText, MalformedRecord, PathText, WireName, WireOmitDefault,
    boundary_decoder,
)
from agent_comms.channel_targets import NamedTarget
from agent_comms.owner_requests import NewestOpenRequest, RequestDirective, RequestSelection
from .host_services import HostService, SystemdUserService
from .connector_store import (
    CODE_SECONDS, ApprovalQueueFull, ApprovalRefused, AuthorizationQuery,
    AuthorizationRequest, ClaudeCallback,
    ConnectorGrants, ConnectorStore, IssuedTokens, RegisteredClient, TrustedBrowser, digest,
)
from agent_comms.messages import QuestionKind
from .commander.standing import UnownedOrderWatch
from .commander_heartbeat import CommanderHeartbeat
from .coordinator import Coordination
from .service_passes import ServicePassStore
from .head_review import HeadReview, PullRequestHead
from .repository_name import RepositoryName
from .todos import DirectiveText, ReviewedHeadVerdict
from .tool_result import ToolResult
from .tool_caller import ThreadCaller, ToolCaller
from .tools import (
    CommsSendTool, CommsVerdictTool, MessagePublished, ReadTool, SendTool,
    TodoChanged, ToolRequest, tool_field,
)

LOOPBACK = "127.0.0.1"
PORT = 18766


@dataclass(frozen=True)
class DirectorCaller(ThreadCaller):
    """claude-ai speaks for the owner; its sends must pass the directive wording check."""

    def require_directive(self, comms: Comms, body: str) -> None:
        """A directive passes DirectiveText. claude-ai directs the commanders, so the
        commander rules review does not gate it."""
        DirectiveText.require(body)

class ConnectorTool(ToolRequest[DirectorCaller], affix="ConnectorTool"):
    """Connector-only declarations are absent from the native/CLI tool registry."""


@dataclass(frozen=True, kw_only=True)
class ConnectorSendTool(SendTool, ConnectorTool, declared_name=CommsSendTool.declared_name):
    request: Annotated[
        RequestSelection,
        *tool_field("Owner request ID; omitted selects the recipient's newest open request"),
    ] = NewestOpenRequest()

    def send_message(
        self, comms: Comms, caller: ToolCaller, target: NamedTarget,
    ) -> MessagePublished:
        store = comms.goals.todos
        requests = self.request.for_message(store, target, comms.registry)
        result = super().send_message(comms, caller, target)
        directive = RequestDirective(result.message.source.seq,
                                     result.message.timestamp + RequestDirective.window)
        for request in requests:
            store.record_directive(request, directive)
        return result


@dataclass(frozen=True, kw_only=True)
class ConnectorVerdictTool(ConnectorTool, declared_name=CommsVerdictTool.declared_name):
    label = "Review PR head"
    description = "Record the connector reviewer's acceptance of the exact PR commit it reviewed."
    id: Annotated[str, *tool_field("Problem ID")]
    operation: Annotated[ReviewedHeadVerdict, *tool_field("Accepted PR-head verdict")]

    def apply(self, comms: Comms, caller: DirectorCaller) -> TodoChanged:
        todo = self.operation.reviewed(
            comms.goals, self.id, comms.registry.require(caller.name).incarnation
        )
        return TodoChanged(todo, todo.strike_count)


@dataclass(frozen=True)
class HeadReviewed(ToolResult):
    repo: RepositoryName
    pr: int
    head: str
    review: HeadReview


@dataclass(frozen=True, kw_only=True)
class ConnectorHeadReviewTool(ConnectorTool, declared_name="comms_review_head"):
    """A pull request with no order still gets the connector reviewer's verdict on its head;
    an order's pull request takes comms_verdict, which also records the verdict on the order."""

    label = "Review PR head without an order"
    description = (
        "Post the connector reviewer's verdict (accepted, or sent_back with its reason) as "
        "agent-comms/review on the exact head commit of a pull request no order holds."
    )
    repo: Annotated[RepositoryName, *tool_field("Repository, owner/name")]
    pr: Annotated[int, *tool_field("Pull request number")]
    head: Annotated[str, GitCommitText, *tool_field("The exact head commit reviewed")]
    review: Annotated[HeadReview, *tool_field("accepted, or sent_back with the reason")]

    def apply(self, comms: Comms, caller: DirectorCaller) -> HeadReviewed:
        head = PullRequestHead(self.repo, self.pr, self.head).current()
        self.review.post(self.repo, head, caller.name, f"#{self.pr}")
        return HeadReviewed(self.repo, self.pr, self.head, self.review)


CALLER = DirectorCaller("claude-ai")  # Owner-typed calls act as this registered bus thread.
PROTOCOL_VERSION = "2025-06-18"


def declared(record: type, values: Mapping[str, object]) -> dict[str, object]:
    """The values a record declares; an OAuth peer may send parameters it does not read."""
    names = {plan.key for plan in FieldCodec.field_plans(record)}
    return {key: value for key, value in values.items() if key in names}


@dataclass
class OAuthError(Exception):
    """An OAuth refusal owns its RFC 6749 response as well as its exception text."""

    error: str
    error_description: str

    def __post_init__(self) -> None:
        super().__init__(self.error_description)

    def response(self) -> web.Response:
        return web.json_response(FieldCodec.encode(self), status=400)


@dataclass(frozen=True)
class ApprovalLink:
    """The approval route's payload, decoded once at the HTTP boundary."""

    request_id: str
    secret: str = field(repr=False)
    route: ClassVar[str] = "/approve/{request_id}/{secret}"


@dataclass(frozen=True)
class ClientRegistration:
    """Dynamic client registration accepts exactly claude.ai's redirect."""

    redirect_uris: tuple[ClaudeCallback]
    client_name: str = "unnamed client"


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

    def exchange(self, grants: ConnectorGrants) -> tuple[ConnectorGrants, IssuedTokens]:
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
            or not issued.live(time.time())
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
class ResourceMetadata:
    resource: str
    authorization_servers: tuple[str, ...]
    bearer_methods_supported: tuple[str, ...] = ("header",)


@dataclass(frozen=True)
class ServerMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str
    response_types_supported: tuple[str, ...] = ("code",)
    grant_types_supported: tuple[str, ...] = field(default_factory=TokenGrant.names)
    code_challenge_methods_supported: tuple[str, ...] = ("S256",)
    token_endpoint_auth_methods_supported: tuple[str, ...] = ("none",)


@dataclass(frozen=True, kw_only=True)
class ClientRegistrationReply(ClientRegistration):
    client_id: str
    client_id_issued_at: int
    grant_types: tuple[str, ...] = field(default_factory=TokenGrant.names)
    response_types: tuple[str, ...] = ("code",)
    token_endpoint_auth_method: str = "none"


@dataclass(frozen=True)
class McpResult:
    """An untagged JSON-RPC result. Ping has no additional fields."""


@dataclass(frozen=True, kw_only=True)
class McpEnvelope:
    jsonrpc: Literal["2.0"] = "2.0"


@dataclass(frozen=True)
class McpReply(McpEnvelope):
    id: int | str
    result: McpResult


@dataclass(frozen=True)
class McpError:
    """A JSON-RPC error object; each kind of refusal declares its own code."""

    message: str


@dataclass(frozen=True)
class ParseError(McpError):
    code: Literal[-32700] = -32700


@dataclass(frozen=True)
class InvalidRequestError(McpError):
    code: Literal[-32600] = -32600


@dataclass(frozen=True)
class MethodNotFoundError(McpError):
    code: Literal[-32601] = -32601


@dataclass(frozen=True)
class McpErrorReply(McpEnvelope):
    """A refusal of a request whose own id cannot be read: JSON-RPC answers it with null."""

    error: McpError
    id: None = None

    @staticmethod
    def refusing(data: object, error: McpError) -> McpErrorReply | AddressedErrorReply:
        """A client never matches a null id to its call, so a readable id is always answered."""
        match data:
            case {"id": int() | str() as request_id}:
                return AddressedErrorReply(error, request_id)
        return McpErrorReply(error)


@dataclass(frozen=True)
class AddressedErrorReply(McpEnvelope):
    """A refusal answering the request it refuses."""

    error: McpError
    id: int | str


@dataclass(frozen=True)
class ToolCapabilities:
    list_changed: Annotated[bool, WireName("listChanged")] = False


@dataclass(frozen=True)
class ServerCapabilities:
    tools: ToolCapabilities = ToolCapabilities()


@dataclass(frozen=True)
class ServerInfo:
    name: str = "agent-comms"
    version: str = "1"


@dataclass(frozen=True)
class InitializeResult(McpResult):
    protocol_version: Annotated[str, WireName("protocolVersion")] = (
        PROTOCOL_VERSION
    )
    capabilities: ServerCapabilities = ServerCapabilities()
    server_info: Annotated[ServerInfo, WireName("serverInfo")] = ServerInfo()


@dataclass(frozen=True)
class ToolAnnotations:
    read_only_hint: Annotated[bool, WireName("readOnlyHint")]


@dataclass(frozen=True)
class ToolDescription:
    name: str
    title: str
    description: str
    input_schema: Annotated[dict[str, JsonValue], WireName("inputSchema")]
    annotations: ToolAnnotations

    @classmethod
    def of(cls, name: str, tool: type[ToolRequest] | type[ConnectorTool]) -> ToolDescription:
        """The name is the key tools/call dispatches on, so listing and calling share it."""
        return cls(name, tool.label, f"{tool.description} {tool.bus_effect}",
                   FieldCodec.record_schema(tool), ToolAnnotations(issubclass(tool, ReadTool)))


@dataclass(frozen=True)
class ToolsListResult(McpResult):
    tools: tuple[ToolDescription, ...]


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, object] = field(default_factory=dict)
    meta: Annotated[dict[str, object], WireName("_meta")] = field(
        default_factory=dict
    )

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
        """An undeclared method is refused by its name before its record is read."""
        match data:
            case {"method": str() as method}:
                cls.member_named(method)
        return FieldCodec.decode(cls, data)

    @property
    def log_name(self) -> str:
        return self.declared_name

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
        return web.json_response(FieldCodec.encode(McpReply(self.id, await self.result(connector))))

    @abstractmethod
    async def result(self, connector: Connector) -> McpResult: ...


@dataclass(frozen=True, kw_only=True)
class InitializeMessage(McpRequest, declared_name="initialize"):
    params: dict[str, object]

    async def result(self, connector: Connector) -> InitializeResult:
        return InitializeResult()


@dataclass(frozen=True, kw_only=True)
class PingMessage(McpRequest, declared_name="ping"):
    params: dict[str, object] = field(default_factory=dict)

    async def result(self, connector: Connector) -> McpResult:
        return McpResult()


@dataclass(frozen=True, kw_only=True)
class ToolsListMessage(McpRequest, declared_name="tools/list"):
    params: dict[str, object] = field(default_factory=dict)

    async def result(self, connector: Connector) -> ToolsListResult:
        return ToolsListResult(tuple(
            ToolDescription.of(name, tool) for name, tool in Connector.exposed().items()
        ))


@dataclass(frozen=True, kw_only=True)
class ToolsCallMessage(McpMessage, declared_name="tools/call"):
    id: int | str
    params: ToolCall

    @property
    def log_name(self) -> str:
        return f"{super().log_name} {self.params.name}"

    async def answer(self, connector: Connector) -> web.Response:
        return web.json_response(
            FieldCodec.encode(ToolsCallReply(self.id, await self.result(connector)))
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
        except (ToolRefusedError, RelationViolationError, CoordinationError) as error:
            logging.exception("Connector tool %s failed", self.params.name)
            return ToolsCallResult.failed(error)


@dataclass(frozen=True)
class TextContent:
    text: str
    type: Literal["text"] = "text"


@dataclass(frozen=True)
class ToolsCallResult(McpResult):
    """A tool's encoded reply, or its failed call encoded as an MCP error result."""

    content: tuple[TextContent, ...]
    is_error: Annotated[bool, WireName("isError")]
    structured_content: Annotated[
        dict[str, JsonValue],
        WireName("structuredContent"),
        WireOmitDefault(),
    ] = field(default_factory=dict)

    @classmethod
    def failed(cls, error: Exception) -> ToolsCallResult:
        return cls((TextContent(CoreError.from_exception(error).model_dump_json()),), True)


@dataclass(frozen=True)
class ToolsCallReply(McpReply):
    result: ToolsCallResult


@dataclass(frozen=True)
class Connector:
    """The MCP endpoint and the OAuth authorization server in front of it."""

    store: ConnectorStore
    issuer: str
    root: Path

    @property
    def resource_metadata_url(self) -> str:
        return f"{self.issuer}/.well-known/oauth-protected-resource"

    @staticmethod
    def exposed() -> dict[str, type[ToolRequest] | type[ConnectorTool]]:
        tools: dict[str, type[ToolRequest] | type[ConnectorTool]] = {
            tool.declared_name: tool for tool in ToolRequest.members_with(ToolRequest)
        }
        tools.update({
            tool.declared_name: tool for tool in ConnectorTool.members_with(ConnectorTool)
        })
        return tools

    @contextmanager
    def opened_wire(self) -> Iterator[Comms]:
        """The connector's caller must exist at startup and at each later call."""
        with client_wire(self.root) as comms:
            comms.registry.require(CALLER.name)
            yield comms

    @contextmanager
    def service_wire(self) -> Iterator[Comms]:
        """A service pass selects its explicit root without borrowing a request route.

        Waiting with a client route borrowed would hold up its replacement. The
        pass inherits the bus's hold wait; requests retain client_wire's fast refusal.
        """
        yield Comms.blocking_reads(self.root)

    def invoke(
        self, tool: type[ToolRequest] | type[ConnectorTool], arguments: Mapping[str, object]
    ) -> dict[str, JsonValue]:
        with self.opened_wire() as comms:
            return tool.invoke(comms, arguments, caller=CALLER)

    def application(self) -> web.Application:
        self.store.read()  # Refuse an unreadable store before binding any port.
        with self.opened_wire() as comms:
            CommanderHeartbeat.register(comms.registry)
        app = web.Application(middlewares=[self.log_route])
        app.cleanup_ctx.append(self.watch_orders)
        app.add_routes(
            [
                web.get("/.well-known/oauth-protected-resource", self.resource_metadata),
                web.get("/.well-known/oauth-protected-resource/mcp", self.resource_metadata),
                web.get("/.well-known/oauth-authorization-server", self.server_metadata),
                web.post("/register", self.register),
                web.get("/authorize", self.authorize, allow_head=False),
                web.get(
                    ApprovalLink.route, self.approve,
                    name=self.approve.__name__, allow_head=False,
                ),
                web.post(ApprovalLink.route, self.confirm),
                web.post("/token", self.token),
                web.post("/mcp", self.mcp),
            ]
        )
        return app

    @web.middleware
    async def log_route(self, request: web.Request, handler: Handler) -> web.StreamResponse:
        """One line per request naming its route template: paths and queries carry one-use secrets
        and OAuth values, and headers carry bearer tokens, so none of them is logged."""
        match request.match_info.route:
            case ResourceRoute(resource=AbstractResource(canonical=template)):
                pass
            case SystemRoute():
                template = "unmatched"
            case _:
                raise TypeError("Unsupported aiohttp route")
        started = time.monotonic()
        status = web.HTTPInternalServerError.status_code
        try:
            response = await handler(request)
            status = response.status
            return response
        except web.HTTPException as refusal:
            status = refusal.status
            raise
        finally:
            elapsed = time.monotonic() - started
            logging.info("%s %s %s %.3fs", request.method, template, status, elapsed)

    async def watch_orders(self, app: web.Application) -> AsyncIterator[None]:
        """Core owns both order maintenance and the commander's changed-rule wakes."""
        passes = ServicePassStore.at(self.root)
        tasks = (
            asyncio.create_task(Coordination.watch_worker(
                UnownedOrderWatch(), self.service_wire, passes)),
            asyncio.create_task(Coordination.watch_worker(
                CommanderHeartbeat(), self.service_wire, passes)),
        )
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task

    async def resource_metadata(self, request: web.Request) -> web.Response:
        return web.json_response(FieldCodec.encode(
            ResourceMetadata(f"{self.issuer}/mcp", (self.issuer,))))

    async def server_metadata(self, request: web.Request) -> web.Response:
        return web.json_response(FieldCodec.encode(ServerMetadata(
            self.issuer, f"{self.issuer}/authorize", f"{self.issuer}/token",
            f"{self.issuer}/register")))

    async def register(self, request: web.Request) -> web.Response:
        try:
            registration = FieldCodec.decode(
                ClientRegistration, declared(ClientRegistration, await request.json())
            )
        except (MalformedRecord, json.JSONDecodeError) as error:
            return OAuthError("invalid_redirect_uri", str(error)).response()
        client_id = secrets.token_urlsafe(16)
        await asyncio.to_thread(
            self.store.update,
            lambda grants: replace(
                grants,
                clients=grants.clients | {client_id: RegisteredClient(registration.client_name)},
            ),
        )
        return web.json_response(FieldCodec.encode(ClientRegistrationReply(
            client_id=client_id, client_id_issued_at=int(time.time()),
            client_name=registration.client_name, redirect_uris=registration.redirect_uris,
        )), status=201)

    async def authorize(self, request: web.Request) -> web.Response:
        """A trusted browser proceeds; an unknown one needs its owner's one-use link."""
        try:
            query = FieldCodec.decode(
                AuthorizationQuery, declared(AuthorizationQuery, request.query)
            )
        except MalformedRecord as error:
            raise web.HTTPBadRequest(text=str(error)) from error
        code, approval = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        browser = request.cookies.get(TrustedBrowser.cookie_name, "")
        notices: list[AuthorizationRequest] = []

        def decided(grants: ConnectorGrants) -> ConnectorGrants:
            now = time.time()
            grants = grants.unexpired(now)
            if query.client_id not in grants.clients:
                raise web.HTTPBadRequest(text="Unknown client.")
            if grants.trusts(browser):
                return grants.code_issued(query, code, now)
            if query.request_id not in grants.requests:
                grants = grants.waiting(query, digest(approval), now)
                notices.append(grants.requests[query.request_id])
            return grants

        try:
            grants = await asyncio.to_thread(self.store.update, decided)
        except ApprovalQueueFull as refusal:
            raise web.HTTPTooManyRequests(
                text=str(refusal), headers={"Retry-After": str(CODE_SECONDS)}
            ) from refusal
        for pending in notices:
            path = request.app.router[self.approve.__name__].url_for(
                **FieldCodec.encode(ApprovalLink(pending.query.request_id, approval))
            )
            await asyncio.to_thread(
                self.request_approval, grants.clients[query.client_id].name, f"{self.issuer}{path}"
            )
        if query.request_id in grants.requests:
            return web.Response(
                content_type="text/html",
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
                text=(
                    "<!doctype html><title>Approve connector</title>"
                    "<p>An approval link was sent to the owner on comms.</p>"
                    "<p>Open that link and confirm to connect and remember this browser.</p>"
                ),
            )
        raise web.HTTPFound(query.redirect(code), headers={"Cache-Control": "no-store"})

    def request_approval(self, client: str, link: str) -> None:
        """Only the durable human identity receives the bearer approval link."""
        with self.opened_wire() as comms:
            owner = comms.messaging.user_identity(str(self.root))
            comms.messaging.send(
                CALLER.name, owner.name,
                f"Connector sign-in requested by {json.dumps(client)}. "
                f"Open the link and confirm only if you started this connection; it expires in "
                f"{CODE_SECONDS} seconds and remembers this browser.\n{link}",
                QuestionKind,
            )

    async def approve(self, request: web.Request) -> web.Response:
        """A preview is read-only; only the form's POST can establish browser trust."""
        link = FieldCodec.decode(ApprovalLink, dict(request.match_info))
        grants = await asyncio.to_thread(self.store.read)
        try:
            held = grants.require_approval(link.request_id, link.secret, time.time())
        except ApprovalRefused as refusal:
            raise web.HTTPBadRequest(text=str(refusal)) from refusal
        client = html.escape(grants.clients[held.query.client_id].name)
        return web.Response(
            content_type="text/html",
            headers={
                "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": (
                    f"default-src 'none'; form-action 'self' {held.query.redirect_uri}; "
                    "frame-ancestors 'none'; base-uri 'none'"
                ),
            },
            text=(f"<!doctype html><title>Approve connector</title><p>Connect {client}?</p>"
                  '<form method="post"><button type="submit">Approve and connect</button></form>'),
        )

    async def confirm(self, request: web.Request) -> web.Response:
        """Consume the owner's POST and establish browser trust in the same update."""
        link = FieldCodec.decode(ApprovalLink, dict(request.match_info))
        code, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        approved: list[AuthorizationQuery] = []

        def consumed(grants: ConnectorGrants) -> ConnectorGrants:
            now = time.time()
            held = grants.require_approval(link.request_id, link.secret, now)
            approved.append(held.query)
            return replace(
                grants.unexpired(now).code_issued(held.query, code, now),
                trusted_browsers=grants.trusted_browsers | {digest(browser): TrustedBrowser(now)},
            )

        try:
            await asyncio.to_thread(self.store.update, consumed)
        except ApprovalRefused as refusal:
            raise web.HTTPBadRequest(text=str(refusal)) from refusal
        response = web.HTTPFound(
            approved[0].redirect(code),
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
        response.set_cookie(
            TrustedBrowser.cookie_name, browser, max_age=TrustedBrowser.cookie_seconds,
            secure=True, httponly=True, samesite="Lax", path="/",
        )
        raise response

    async def token(self, request: web.Request) -> web.Response:
        try:
            grant = TokenGrant.received(await request.post())
        except MalformedRecord as error:
            return OAuthError("invalid_request", str(error)).response()
        exchanged: list[IssuedTokens] = []

        def spent(grants: ConnectorGrants) -> ConnectorGrants:
            grants, response = grant.exchange(grants)
            exchanged.append(response)
            return grants

        try:
            await asyncio.to_thread(self.store.update, spent)
        except OAuthError as refusal:
            return refusal.response()
        return web.json_response(
            FieldCodec.encode(exchanged[0]), headers={"Cache-Control": "no-store"}
        )

    @staticmethod
    def refused(data: object, error: McpError) -> web.Response:
        logging.warning("MCP request refused (%s): %s", type(error).__name__, error.message)
        return web.json_response(FieldCodec.encode(McpErrorReply.refusing(data, error)))

    async def mcp(self, request: web.Request) -> web.Response:
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        grants = await asyncio.to_thread(self.store.read)
        if not grants.holds(token):
            raise web.HTTPUnauthorized(
                headers={
                    "WWW-Authenticate": (f'Bearer resource_metadata="{self.resource_metadata_url}"')
                }
            )
        try:
            data = await request.json()
        except json.JSONDecodeError as error:
            return self.refused(None, ParseError(str(error)))
        try:
            message = McpMessage.received(data)
        except UnknownFamilyMember as error:
            return self.refused(data, MethodNotFoundError(str(error)))
        except MalformedRecord as error:
            return self.refused(data, InvalidRequestError(str(error)))
        started = time.monotonic()
        response = await message.answer(self)
        logging.info("MCP %s answered in %.3fs", message.log_name, time.monotonic() - started)
        return response


@dataclass(frozen=True, kw_only=True)
class ConnectorVerb(
    DeclaredVerb, Command,
    affix="Verb", discovery_package=HostService.__module__,
):
    """One connector command: its options are its fields, and its parser comes from them."""

    program: ClassVar[str] = "agent-comms-connector"

    @abstractmethod
    def apply(self, ctx: ConnectorStore) -> int | str: ...

    @classmethod
    def tool(cls) -> str:
        return cls.program


@dataclass(frozen=True, kw_only=True)
class ConnectorOptions(ConnectorVerb, declared_name=None):
    """The endpoint's CLI fields and their declared argument spelling."""

    root: Annotated[
        Path,
        PathText,
        Argument(flags=("--root",), options={"help": "Explicit Comms root to serve"}),
    ]
    port: Annotated[
        int,
        Argument(
            flags=("--port",), options={"help": "The loopback port it listens on"}
        ),
    ] = PORT
    issuer: Annotated[
        str,
        Argument(
            flags=("--issuer",), options={"help": "Public origin the OAuth metadata names"}
        ),
    ] = f"http://{LOOPBACK}:{PORT}"

    def command_arguments(self) -> tuple[str, ...]:
        return (self.declared_name, *(word for plan in FieldCodec.field_plans(type(self))
                for word in (Argument.declared(plan.annotation).flags_for(plan.name)[0],
                             str(plan.current(self)))))


@dataclass(frozen=True, kw_only=True)
class ServeVerb(ConnectorOptions):
    help = f"Serve MCP on {LOOPBACK}:{PORT}/mcp"

    def apply(self, ctx: ConnectorStore) -> int | str:
        connector = Connector(ctx, self.issuer.rstrip("/"), self.root)
        try:
            app = connector.application()
        except UnregisteredThreadError:
            return f"First run agent-comms register --name {CALLER.name} --worktree DIR"
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
        asyncio.run(self.serve(app))
        return 0

    async def serve(self, app: web.Application) -> None:
        """Bind, then report ready, so a restart returns only once the port answers."""
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stopped.set)
        try:
            await web.TCPSite(runner, LOOPBACK, self.port).start()
            SystemdUserService.ready()
            await stopped.wait()
        finally:
            await runner.cleanup()


@dataclass(frozen=True, kw_only=True)
class RevokeVerb(ConnectorVerb):
    help = "Revoke every issued token, code and trusted browser"

    def apply(self, ctx: ConnectorStore) -> int | str:
        ctx.update(lambda grants: replace(
            grants, requests={}, codes={}, access={}, refresh={}, trusted_browsers={}
        ))
        return 0


@boundary_decoder
def main(argv: Sequence[str] | None = None) -> int | str:
    arguments = sys.argv[1:] if argv is None else argv
    return ConnectorVerb.parse(arguments).apply(ConnectorStore.owned())
