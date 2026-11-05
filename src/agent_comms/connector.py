"""agent-comms-connector: the claude.ai connector, a Streamable HTTP MCP server beside Core.

The server is a transport over the ToolRequest family: ``tools/list`` reports every member
and whether it changes the bus; ``tools/call`` runs ``invoke()`` as the caller identified
by its bearer grant.
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

from agent_comms.nominal.field_codec import WireExclude


import asyncio
import base64
import hashlib
import html
import json
import logging
import multiprocessing
import os
import secrets
import signal
import socket
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, ClassVar, Literal, Self, cast

from agent_comms_hooks import ToolRefusedError
from aiohttp import web
from aiohttp.typedefs import Handler
from aiohttp.web_urldispatcher import AbstractResource, ResourceRoute, SystemRoute
from yarl import URL
from pydantic import JsonValue


from .declared_verb import DeclaredVerb
from .cli_protocol import CoreError
from .command import Command
from .child_process import GitChildren, OwnedGitOpening, join_retirement
from .active_route import CommsRoute, resolve_comms_route
from .comms import Comms, client_wire, wire
from .ui_model.observation import RelayConnection, relay_frames, serve_encoded
from .repo import Checkout
from .nominal.declared_family import DeclaredFamily, UnknownFamilyMember
from .errors import (
    RelationViolationError,
)
from .coordination.errors import CommandFailure, Refused
from .nominal.field_codec import (
    Argument, FieldCodec, GitCommitText, MalformedRecord, NullMember, PathText, SingleFieldValue,
    SubclassUnion, WireName, WireOmitDefault, WireTransient, RecordMember, boundary_decoder,
    projected,
)
from .exporting import CancellableWireExport, WireExportCancelled, WireExportObserver, WireExportProgress
from agent_comms.channel_targets import NamedTarget
from agent_comms.owner_requests import NewestOpenRequest, RequestSelection
from .host_services import HostService
from .connector_caller import CALLER, ConnectorCaller, ConnectorTool, GrantRefusedError
from .connector_profile import HttpsMcpEndpoint, UiGrantId
from .registry_store import RegistryStore
from .connector_store import (
    CODE_SECONDS, ApprovalQueueFull, ApprovalRefused, AuthorizationQuery,
    AuthorizationRequest, ClaudeCallback,
    ConnectorGrants, ConnectorStore, IssuedTokens,
    RegisteredClient, TrustedBrowser, UiGrantAccess, UiReadAccess, digest,
)
from .storage.locked_store import DocumentChange
from .messages import QuestionKind
from .team.standing import UnownedOrderWatch
from .team_heartbeat import TeamHeartbeat
from .closed_pull_requests import ClosedPullRequestWatch
from .message_bus import Watch
from .coordination.coordinator import Coordination
from .service_passes import ServicePassStore
from .tool_result import ToolResult
from .task_sources import OwnerRequestAttachment
from .inbox_watch import InboxWatch, OpenWatch, WatchRoute, WatchTickets
from .tool_caller import ThreadCaller
from .tools import CommsSendTool, SendTool
from .order_tools import CommsHeadVerdictTool, HeadVerdictTool, MessagePublished
from .tool_request import ConnectorService, NoConnectorService, ToolCall, ToolRequest, tool_field

JsonRpcId = int | str
"""A JSON-RPC request id, as the client chose it."""


LOOPBACK = "127.0.0.1"
PORT = 18766


class SystemdSite(web.TCPSite):
    """The listening site notifies its supervisor only after binding succeeds."""

    notify_variable: ClassVar[str] = "NOTIFY_SOCKET"  # systemd's supervisor socket

    async def start(self) -> None:
        await super().start()
        address = os.environ[self.notify_variable]
        target = "\0" + address[1:] if address.startswith("@") else address
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notify:
            notify.connect(target)
            notify.sendall(b"READY=1")


@dataclass(frozen=True, kw_only=True)
class ConnectorSendTool(SendTool, ConnectorTool, declared_name=CommsSendTool.declared_name):
    request: Annotated[
        RequestSelection,
        *tool_field("Owner request ID; omitted selects the recipient's newest open request"),
    ] = NewestOpenRequest()

    def attachment(self, comms: Comms, target: NamedTarget) -> OwnerRequestAttachment:
        requests = self.request.for_message(comms.goals.todos, target, comms.registry)
        return OwnerRequestAttachment(tuple(request.id for request in requests))


@dataclass(frozen=True, kw_only=True)
class ConnectorWatchTool(ConnectorTool, declared_name="comms_watch"):
    label = "Watch Inbox"
    description = (
        "Get a one-use wss:// URL that pushes your inbox, so a message wakes this session. Open it "
        "at once by running agent-comms-inbox-watch URL under your Monitor tool, with the longest "
        "timeout; from an agent-comms checkout, uv run --locked --no-dev agent-comms-inbox-watch URL. "
        "Reading your inbox is not this: only a running watch wakes you. Each message "
        "arrives as an event naming a file to read, and your thread shows as bridged while it runs. "
        "The URL expires in a minute. When the Monitor expires or the watch says it ended, call "
        "this again. Never post the URL anywhere."
    )
    service: Annotated[ConnectorService, WireTransient()] = field(
        default_factory=NoConnectorService, compare=False)

    def served_by(self, service: ConnectorService) -> Self:
        return replace(self, service=service)

    def apply(self, comms: Comms, caller: ThreadCaller) -> ToolResult:
        return self.service.watch_url(caller.identity(comms).name)


@dataclass(frozen=True, kw_only=True)
class ConnectorHeadVerdictTool(HeadVerdictTool, ConnectorTool,
                               declared_name=CommsHeadVerdictTool.declared_name):
    """Director and native callers share the same persisted verdict and publication path."""


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

    def exchange(self, grants: ConnectorGrants) -> DocumentChange[ConnectorGrants, IssuedTokens]:
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
class McpResult(RecordMember):
    """An untagged JSON-RPC result. Ping has no additional fields."""


@dataclass(frozen=True, kw_only=True)
class McpEnvelope:
    jsonrpc: Literal["2.0"] = "2.0"


@dataclass(frozen=True)
class McpReply(McpEnvelope):
    id: JsonRpcId
    result: McpResult


@dataclass(frozen=True)
class McpError(RecordMember):
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
    @projected(view=WireExclude)
    def code(self) -> int:
        return Refused.METHOD_NOT_FOUND


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
    id: JsonRpcId


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
    instructions: str = (
        "Several Claude sessions can share one connector grant. When a tool offers the fork "
        "argument, choose one distinct name for this session and pass {\"name\": \"...\"} on "
        "every call, so teammates can tell you apart: your sends come from that name and your "
        "inbox (comms_inbox with thread set to it) holds only what is sent to it."
    )


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
    def of(
        cls, name: str, tool: type[ToolRequest] | type[ConnectorTool], caller: ConnectorCaller
    ) -> ToolDescription:
        """The name is the key tools/call dispatches on, so listing and calling share it."""
        return cls(name, tool.label, f"{tool.description} {tool.bus_effect}",
                   caller.input_schema(tool), ToolAnnotations(tool.read_only_hint))


@dataclass(frozen=True)
class ToolsListResult(McpResult):
    tools: tuple[ToolDescription, ...]


SESSION_HEADER = "Mcp-Session-Id"


@dataclass(frozen=True)
class McpSession:
    """This connector process's MCP session. A client that initialized against an earlier
    process holds an id this one never issued, so it is answered 404 and initializes again,
    which makes it list the tools this process serves: the tools an install added reach a
    session that was open across it."""

    id: str = field(default_factory=lambda: secrets.token_hex(16))

    def claimed(self, request: web.Request) -> SessionClaim:
        """The header is foreign until it is compared with the id issued here."""
        match request.headers.get(SESSION_HEADER):
            case None:
                return UnclaimedSession()
            case self.id:
                return CurrentSession()
            case _:
                return PastSession()


class SessionClaim:
    """What a request's session header says about the process answering it."""

    async def answer(
        self, message: McpMessage, connector: Connector, caller: ConnectorCaller,
        http: web.Request,
    ) -> web.StreamResponse:
        return await message.answer(connector, caller, http)


class UnclaimedSession(SessionClaim):
    """No header: a client that has not initialized yet, or one that keeps no session."""


class CurrentSession(SessionClaim):
    """The id this process issued."""


class PastSession(SessionClaim):
    """An id from an earlier process: its tool list may be missing what this one serves."""

    async def answer(
        self, message: McpMessage, connector: Connector, caller: ConnectorCaller,
        http: web.Request,
    ) -> web.StreamResponse:
        return await message.past_session(connector, caller, http)


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
    async def answer(
        self, connector: Connector, caller: ConnectorCaller, http: web.Request,
    ) -> web.StreamResponse: ...

    async def past_session(
        self, connector: Connector, caller: ConnectorCaller, http: web.Request,
    ) -> web.StreamResponse:
        """The session this message names ended with the process that issued it."""
        return web.Response(status=404)


@dataclass(frozen=True, kw_only=True)
class McpNotification(McpMessage, declared_name=None):
    """A notification gets no reply."""

    params: dict[str, object] = field(default_factory=dict)

    async def answer(
        self, connector: Connector, caller: ConnectorCaller, http: web.Request,
    ) -> web.StreamResponse:
        return web.Response(status=202)


@dataclass(frozen=True, kw_only=True)
class InitializedMessage(McpNotification, declared_name="notifications/initialized"):
    pass


@dataclass(frozen=True)
class CancelledParams:
    request_id: Annotated[JsonRpcId, WireName("requestId")]


@dataclass(frozen=True, kw_only=True)
class CancelledMessage(McpNotification, declared_name="notifications/cancelled"):
    """The client stops one of its running calls; a call that cannot stop finishes as usual."""

    async def answer(
        self, connector: Connector, caller: ConnectorCaller, http: web.Request,
    ) -> web.StreamResponse:
        connector.cancel_call(FieldCodec.decode(CancelledParams, self.params).request_id)
        return web.Response(status=202)


@dataclass(frozen=True, kw_only=True)
class McpRequest(McpMessage):
    id: JsonRpcId

    async def answer(
        self, connector: Connector, caller: ConnectorCaller, http: web.Request,
    ) -> web.StreamResponse:
        return web.json_response(
            FieldCodec.encode(McpReply(self.id, await self.result(connector, caller)))
        )

    @abstractmethod
    async def result(self, connector: Connector, caller: ConnectorCaller) -> McpResult: ...


@dataclass(frozen=True, kw_only=True)
class InitializeMessage(McpRequest, declared_name="initialize"):
    params: dict[str, object]

    async def answer(
        self, connector: Connector, caller: ConnectorCaller, http: web.Request,
    ) -> web.StreamResponse:
        response = await super().answer(connector, caller, http)
        response.headers[SESSION_HEADER] = connector.session.id
        return response

    async def past_session(
        self, connector: Connector, caller: ConnectorCaller, http: web.Request,
    ) -> web.StreamResponse:
        """Initializing is how a past session's client starts this process's session."""
        return await self.answer(connector, caller, http)

    async def result(self, connector: Connector, caller: ConnectorCaller) -> InitializeResult:
        return InitializeResult()


@dataclass(frozen=True, kw_only=True)
class PingMessage(McpRequest, declared_name="ping"):
    params: dict[str, object] = field(default_factory=dict)

    async def result(self, connector: Connector, caller: ConnectorCaller) -> McpResult:
        return McpResult()


@dataclass(frozen=True, kw_only=True)
class ToolsListMessage(McpRequest, declared_name="tools/list"):
    params: dict[str, object] = field(default_factory=dict)

    async def result(self, connector: Connector, caller: ConnectorCaller) -> ToolsListResult:
        return ToolsListResult(tuple(
            ToolDescription.of(name, tool, caller) for name, tool in caller.exposed().items()
        ))


class ProgressRequest(SubclassUnion, affix="Progress"):
    """Whether a tools/call asked for progress: no token (null) or the client's token."""

    @abstractmethod
    async def answered(
        self, message: ToolsCallMessage, connector: Connector, caller: ConnectorCaller,
        http: web.Request,
    ) -> web.StreamResponse: ...


@dataclass(frozen=True)
class NoProgress(NullMember, ProgressRequest):
    async def answered(
        self, message: ToolsCallMessage, connector: Connector, caller: ConnectorCaller,
        http: web.Request,
    ) -> web.StreamResponse:
        return web.json_response(FieldCodec.encode(
            ToolsCallReply(message.id, await message.result(connector, caller, WireExportObserver()))))


@dataclass(frozen=True)
class ProgressToken(SingleFieldValue, ProgressRequest):
    """MCP's progressToken is a string or a number; Claude Code sends a number."""

    token: JsonRpcId

    async def answered(
        self, message: ToolsCallMessage, connector: Connector, caller: ConnectorCaller,
        http: web.Request,
    ) -> web.StreamResponse:
        """Stream progress events while the call runs, then its reply, as server-sent events."""
        stream = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await stream.prepare(http)
        observer = StreamedProgress(asyncio.get_running_loop())
        connector.observed[message.id] = observer
        try:
            call = asyncio.create_task(message.result(connector, caller, observer))
            while True:
                progressed = asyncio.create_task(observer.events.get())
                await asyncio.wait((call, progressed), return_when=asyncio.FIRST_COMPLETED)
                if not progressed.done():
                    progressed.cancel()
                    break
                await stream.write(ProgressNotice.sent(self.token, progressed.result()))
            while not observer.events.empty():
                await stream.write(ProgressNotice.sent(self.token, observer.events.get_nowait()))
            await stream.write(StreamedEvent.framed(FieldCodec.encode(ToolsCallReply(message.id, call.result()))))
        finally:
            connector.observed.pop(message.id, None)
        await stream.write_eof()
        return stream


class StreamedProgress(CancellableWireExport):
    """A connector-side export observer: each advance becomes one streamed event."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__()
        self.loop = loop
        self.events: asyncio.Queue[WireExportProgress] = asyncio.Queue()

    def advanced(self, progress: WireExportProgress) -> None:
        self.loop.call_soon_threadsafe(self.events.put_nowait, progress)


@dataclass(frozen=True)
class ToolCallMeta:
    """The part of a call's `_meta` Core reads. MCP's `_meta` is open: a client adds its own
    namespaced keys (Claude Code's claudecode/toolUseId), and those are not Core's to refuse."""

    progress: Annotated[ProgressRequest, WireName("progressToken")] = NoProgress()

    @classmethod
    def of(cls, meta: Mapping[str, object]) -> ToolCallMeta:
        declared = {entry.key for entry in FieldCodec.argument_fields(cls)}
        return FieldCodec.decode(cls, {key: meta[key] for key in declared & meta.keys()})


class StreamedEvent(ABC):
    """One server-sent event of a streamed call: a progress notice or the final reply."""

    @abstractmethod
    def delivered(self, observer: WireExportObserver, replies: list[ToolsCallReply]) -> None: ...

    @staticmethod
    def framed(encoded: object) -> bytes:
        return f"data: {FieldCodec.compact(encoded)}\n\n".encode()


@dataclass(frozen=True)
class ExportProgressMeta:
    export: Annotated[WireExportProgress, WireName("agent_comms/export")]


@dataclass(frozen=True)
class ProgressParams:
    token: Annotated[JsonRpcId, WireName("progressToken")]
    progress: int
    meta: Annotated[ExportProgressMeta, WireName("_meta")]


@dataclass(frozen=True)
class ProgressNotice(StreamedEvent):
    jsonrpc: Literal["2.0"]
    method: Literal["notifications/progress"]
    params: ProgressParams

    def delivered(self, observer: WireExportObserver, replies: list[ToolsCallReply]) -> None:
        observer.advanced(self.params.meta.export)

    @classmethod
    def sent(cls, token: JsonRpcId, progress: WireExportProgress) -> bytes:
        notice = cls("2.0", "notifications/progress",
                     ProgressParams(token, progress.messages_written, ExportProgressMeta(progress)))
        return cls.framed(FieldCodec.encode(notice))


@dataclass(frozen=True, kw_only=True)
class ToolsCallMessage(McpMessage, declared_name="tools/call"):
    id: JsonRpcId
    params: ToolCall

    @property
    def log_name(self) -> str:
        return f"{super().log_name} {self.params.name}"

    async def answer(
        self, connector: Connector, caller: ConnectorCaller, http: web.Request,
    ) -> web.StreamResponse:
        """The call's own meta says whether the client asked for progress."""
        meta = ToolCallMeta.of(self.params.meta)
        return await meta.progress.answered(self, connector, caller, http)

    async def result(
        self, connector: Connector, caller: ConnectorCaller, observer: WireExportObserver,
    ) -> ToolsCallResult:
        tool = caller.exposed().get(self.params.name)
        if tool is None:
            return ToolsCallResult.failed(
                ToolRefusedError(f"The connector exposes no tool {self.params.name!r}.")
            )
        try:
            reply = await connector.call(tool, self.params.arguments, caller, observer)
            return ToolsCallResult((TextContent(json.dumps(reply)),), False, reply)
        except (
            # Invalid input (ToolRefusedError, MalformedRecord, an unrecorded time) and any
            # declared CommandFailure (git, Refused, coordination) are the caller's refusal;
            # escaping here answers HTTP 500, which the client sees as a lost connection.
            ValueError, CommandFailure, ConnectionError, TimeoutError, WireExportCancelled,
        ) as error:
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
class ToolsCallReply(McpReply, StreamedEvent):
    result: ToolsCallResult

    def structured(self) -> dict[str, JsonValue]:
        """The tool's declared reply, or its declared failure raised."""
        if self.result.is_error:
            raise RelationViolationError(" ".join(content.text for content in self.result.content))
        return self.result.structured_content

    def delivered(self, observer: WireExportObserver, replies: list[ToolsCallReply]) -> None:
        replies.append(self)


@dataclass(frozen=True)
class StreamedLine:
    """One line of a streamed call; only server-sent `data:` lines carry an event."""

    line: bytes

    @boundary_decoder
    def delivered(self, observer: WireExportObserver, replies: list[ToolsCallReply]) -> None:
        text = self.line.decode().strip()
        if text.startswith("data: "):
            event = FieldCodec.decode(ProgressNotice | ToolsCallReply, json.loads(text[len("data: "):]))
            event.delivered(observer, replies)


@dataclass(frozen=True)
class Connector(ConnectorService):
    """The MCP endpoint and the OAuth authorization server in front of it."""

    store: ConnectorStore
    issuer: str
    root: Path
    watches: Mapping[type[Watch], Watch] = field(
        default_factory=lambda: MappingProxyType({member: member() for member in Watch.members_with(Watch)}),
        init=False, repr=False, compare=False,
    )

    calls: dict[
        asyncio.Task[dict[str, JsonValue]], type[ToolRequest] | type[ConnectorTool],
    ] = field(
        default_factory=dict, init=False, repr=False, compare=False,
    )
    observed: dict[JsonRpcId, CancellableWireExport] = field(
        default_factory=dict, init=False, repr=False, compare=False,
    )
    git_children: GitChildren = field(default_factory=GitChildren, init=False, repr=False, compare=False)

    watch_tasks: set[asyncio.Task[None]] = field(
        default_factory=set, init=False, repr=False, compare=False,
    )
    open_watches: list[OpenWatch] = field(
        default_factory=list, init=False, repr=False, compare=False,
    )

    watch_tickets: WatchTickets = field(
        default_factory=WatchTickets, init=False, repr=False, compare=False,
    )
    session: McpSession = field(
        default_factory=McpSession, init=False, repr=False, compare=False,
    )

    def watch_url(self, thread: str) -> ToolResult:
        return self.watch_tickets.issued(thread, URL(self.issuer))

    async def inbox_watch(self, request: web.Request) -> web.WebSocketResponse:
        """The ticket in the path is the only credential; log_route logs the route's template.
        A newer socket for a thread replaces the older one, as a re-armed watch does."""
        route = FieldCodec.decode(WatchRoute, dict(request.match_info))
        thread = self.watch_tickets.taken(route.ticket).thread
        # A transcript line holds whatever a tool returned, far past aiohttp's 4 MiB default.
        viewer = web.WebSocketResponse(heartbeat=25.0, max_msg_size=64 * 1024 * 1024)
        await viewer.prepare(request)
        watch = OpenWatch(thread, viewer)
        replaced = tuple(held for held in self.open_watches if held.thread == thread)
        self.open_watches.append(watch)
        for held in replaced:
            await held.viewer.close()
        try:
            await InboxWatch(thread, self.root).carried(viewer)
        finally:
            self.open_watches.remove(watch)
        return viewer

    def log_pending(self) -> None:
        """The existing task owners name pending work; idle watches may also be listed."""
        logging.info("Connector stopping; pending tools: %s; pending watches: %s",
                     ", ".join(tool.declared_name for task, tool in self.calls.items()
                               if not task.done()) or "none",
                     ", ".join(sorted(task.get_name() for task in self.watch_tasks
                                      if not task.done())) or "none")

    async def call(
        self, tool: type[ToolRequest] | type[ConnectorTool],
        arguments: Mapping[str, object], caller: ConnectorCaller, observer: WireExportObserver,
    ) -> dict[str, JsonValue]:
        """Start once; a disconnected HTTP client cannot abandon its write."""
        if self.git_children.closing.is_set():
            raise web.HTTPServiceUnavailable(text="Connector is stopping")
        with OwnedGitOpening(self.git_children).bound():
            pending = asyncio.create_task(tool.run_async(partial(self.invoke, tool, arguments, caller, observer)))
        self.calls[pending] = tool
        pending.add_done_callback(self.calls.pop)
        return await asyncio.shield(pending)

    async def begin_shutdown(self, app: web.Application) -> None:
        """Close acquisition launches before any shutdown hook can join entered work."""
        retirement = self.git_children.start_shutdown()
        for task in self.watch_tasks:
            task.cancel()
        for watch in tuple(self.open_watches):
            await watch.viewer.close()
        await join_retirement(retirement)

    async def stop_calls(self, app: web.Application) -> None:
        """The runner has stopped accepting: drop readers and join declared mutations."""
        pending = tuple(self.calls.items())
        for call, tool in pending:
            tool.cancel_on_shutdown(call)
        await asyncio.gather(*(call for call, tool in pending), return_exceptions=True)

    @property
    def heartbeat(self) -> TeamHeartbeat:
        return cast(TeamHeartbeat, self.watches[TeamHeartbeat])

    @property
    def resource_metadata_url(self) -> str:
        return f"{self.issuer}/.well-known/oauth-protected-resource"

    @contextmanager
    def opened_wire(self) -> Iterator[Comms]:
        """Open data at the served root; each caller checks its own access."""
        with client_wire(self.root) as comms:
            yield comms

    @contextmanager
    def service_wire(self) -> Iterator[Comms]:
        """A service pass selects its explicit root without borrowing a request route.

        Waiting with a client route borrowed would hold up its replacement. The
        pass inherits the bus's hold wait; requests retain client_wire's fast refusal.
        """
        yield wire(self.root)

    def invoke(
        self, tool: type[ToolRequest] | type[ConnectorTool], arguments: Mapping[str, object],
        caller: ConnectorCaller, observer: WireExportObserver,
    ) -> dict[str, JsonValue]:
        tool.require_entry_open(self.root)
        with self.opened_wire() as comms:
            acting, rest = caller.acting(arguments)
            caller.prepare_call(comms, acting)
            return tool.decoded(comms, rest).bound(observer).served_by(self).replied(comms, acting)

    def cancel_call(self, request_id: JsonRpcId) -> None:
        """A cancel names a running observed call by its request id; any other call runs on."""
        self.observed.get(request_id, CancellableWireExport()).cancel()

    def application(self) -> web.Application:
        self.store.read()  # Refuse an unreadable store before binding any port.
        app = web.Application(middlewares=[self.log_route])
        app.cleanup_ctx.append(self.watch_orders)
        app.on_shutdown.append(self.begin_shutdown)
        app.on_shutdown.append(self.stop_calls)
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
                web.get("/observe", self.observe),
                web.get("/watch/{ticket}", self.inbox_watch),
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
        """Core owns both order maintenance and the teammate's changed-rule wakes."""
        passes = ServicePassStore.at(self.root)
        with OwnedGitOpening(self.git_children).bound():
            self.watch_tasks.update(
                asyncio.create_task(Coordination.watch_worker(
                    watch, self.service_wire, passes), name=type(watch).__name__)
                for watch in self.watches.values()
            )
        try:
            yield
        finally:
            for task in self.watch_tasks:
                task.cancel()
            for task in self.watch_tasks:
                with suppress(asyncio.CancelledError):
                    await task
            self.watch_tasks.clear()

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
            lambda grants: DocumentChange.to(replace(
                grants,
                clients=grants.clients | {client_id: RegisteredClient(registration.client_name)},
            )),
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

        def decided(grants: ConnectorGrants) -> DocumentChange[ConnectorGrants, ConnectorGrants]:
            now = time.time()
            grants = grants.unexpired(now)
            if query.client_id not in grants.clients:
                raise web.HTTPBadRequest(text="Unknown client.")
            if grants.trusts(browser):
                return DocumentChange.to(grants.code_issued(query, code, now))
            if query.request_id not in grants.requests:
                grants = grants.waiting(query, digest(approval), now)
                notices.append(grants.requests[query.request_id])
            return DocumentChange.to(grants)

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
            owner = comms.messaging.user_identity(Checkout(self.root))
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

        def consumed(grants: ConnectorGrants) -> DocumentChange[ConnectorGrants, AuthorizationQuery]:
            now = time.time()
            held = grants.require_approval(link.request_id, link.secret, now)
            return DocumentChange(replace(
                grants.unexpired(now).code_issued(held.query, code, now),
                trusted_browsers=grants.trusted_browsers | {digest(browser): TrustedBrowser(now)},
            ), held.query)

        try:
            approved = await asyncio.to_thread(self.store.update, consumed)
        except ApprovalRefused as refusal:
            raise web.HTTPBadRequest(text=str(refusal)) from refusal
        response = web.HTTPFound(
            approved.redirect(code),
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
        try:
            exchanged = await asyncio.to_thread(self.store.update, grant.exchange)
        except OAuthError as refusal:
            return refusal.response()
        return web.json_response(
            FieldCodec.encode(exchanged), headers={"Cache-Control": "no-store"}
        )

    @staticmethod
    def refused(data: object, error: McpError) -> web.Response:
        logging.warning("MCP request refused (%s): %s", type(error).__name__, error.message)
        return web.json_response(FieldCodec.encode(McpErrorReply.refusing(data, error)))

    def caller_for(self, token: str) -> ConnectorCaller:
        """Resolve the bearer and its root authority before reading any MCP message."""
        caller = self.store.read().caller_for(token)
        caller.require_root(self.root)
        return caller

    async def bearer_caller(self, request: web.Request) -> ConnectorCaller:
        """The request's bearer grant, or the OAuth challenge every route answers with."""
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        try:
            return await asyncio.to_thread(self.caller_for, token)
        except GrantRefusedError:
            raise web.HTTPUnauthorized(
                headers={
                    "WWW-Authenticate": (f'Bearer resource_metadata="{self.resource_metadata_url}"')
                }
            )

    async def observe(self, request: web.Request) -> web.WebSocketResponse:
        """A UI grant's observation of its root, carried as declared records."""
        caller = await self.bearer_caller(request)
        try:
            caller.require_observation()
        except GrantRefusedError as error:
            raise web.HTTPForbidden(text=str(error))
        viewer = web.WebSocketResponse()
        await viewer.prepare(request)
        await ObservationRelay(resolve_comms_route(self.root)).carry(viewer)
        return viewer

    async def mcp(self, request: web.Request) -> web.StreamResponse:
        caller = await self.bearer_caller(request)
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
        response = await self.session.claimed(request).answer(message, self, caller, request)
        logging.info("MCP %s answered in %.3fs", message.log_name, time.monotonic() - started)
        return response


class ObservationRelay:
    """One remote viewer's observation process. Its frames pass through unopened: the
    service end decodes each one as a declared record and refuses anything else."""

    def __init__(self, route: CommsRoute) -> None:
        relay, service = socket.socketpair()
        with service:
            self.process = multiprocessing.get_context("spawn").Process(
                target=serve_encoded, args=(route, service), daemon=False,
                name="agent-comms-remote-observation")
            self.process.start()
        self.connection = RelayConnection(relay.detach())

    async def carry(self, viewer: web.WebSocketResponse) -> None:
        """Relay frames both ways until either side ends, then close the service's connection,
        which ends it, and retire the process."""
        try:
            await relay_frames(self.connection, viewer)
        finally:
            self.connection.close()
            await asyncio.to_thread(self.retire)

    exit_seconds: ClassVar[float] = 5

    def retire(self) -> None:
        """The service exits once its connection closes; one that has not is hung, and is
        killed so no observation process outlives its viewer."""
        self.process.join(self.exit_seconds)
        if self.process.is_alive():
            self.process.kill()
            self.process.join()


@dataclass(frozen=True, kw_only=True)
class ConnectorVerb(
    DeclaredVerb, Command[ConnectorStore, int | str],
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
class ConnectorRootOptions(ConnectorVerb, declared_name=None):
    root: Annotated[
        Path,
        PathText,
        Argument(flags=("--root",), options={"help": "Explicit Comms root"}),
    ]


@dataclass(frozen=True, kw_only=True)
class ConnectorOptions(ConnectorRootOptions, declared_name=None):
    """The endpoint's CLI fields and their declared argument spelling."""

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
        app = connector.application()
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
        asyncio.run(self.serve(app, connector))
        return 0

    async def serve(self, app: web.Application, connector: Connector) -> None:
        """Bind, then report ready, so a restart returns only once the port answers."""
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stopped.set)
        try:
            await SystemdSite(runner, LOOPBACK, self.port).start()
            await stopped.wait()
            connector.log_pending()
        finally:
            await runner.cleanup()


@dataclass(frozen=True, kw_only=True)
class GrantUiVerb(ConnectorRootOptions, declared_name="grant-ui"):
    help = "Provision a private, expiring UI profile with the selected server authority"

    endpoint: Annotated[
        HttpsMcpEndpoint,
        Argument(flags=("--endpoint",), options={"help": "Public HTTPS MCP resource URL"}),
    ]
    profile: Annotated[
        Path, PathText,
        Argument(flags=("--profile",), options={"help": "New private profile; never overwritten"}),
    ]
    seconds: Annotated[
        int, Argument(flags=("--seconds",), options={
            "help": f"Lifetime, at most {UiGrantAccess.maximum_seconds} seconds"}),
    ] = UiGrantAccess.default_seconds
    access: Annotated[
        type[UiGrantAccess],
        Argument(flags=("--access",), options={"help": "Declared UI grant capability"}),
    ] = UiReadAccess

    def apply(self, ctx: ConnectorStore) -> int | str:
        """The grant is output, printed like round-trip's; a returned string is a refusal."""
        namespace = RegistryStore.at(self.root).privacy().namespace
        profile = ctx.publish_ui_profile(
            namespace, self.endpoint, self.profile, self.seconds, self.access)
        print(FieldCodec.compact(FieldCodec.encode(profile), allow_nan=False))
        return 0


@dataclass(frozen=True, kw_only=True)
class RevokeUiVerb(ConnectorVerb, declared_name="revoke-ui"):
    help = "Revoke one UI grant by public ID, leaving other grants untouched"

    grant_id: Annotated[
        UiGrantId, Argument(flags=("--grant",), options={"help": "Public UI grant ID, not its secret"}),
    ]

    def apply(self, ctx: ConnectorStore) -> int | str:
        ctx.revoke_ui(self.grant_id)
        return 0


@dataclass(frozen=True, kw_only=True)
class RevokeThreadVerb(ConnectorVerb, declared_name="revoke-thread"):
    help = "Revoke one thread's token, leaving every other grant untouched"

    thread: Annotated[str, Argument(flags=("--thread",), options={"help": "Bus thread whose token to revoke"})]

    def apply(self, ctx: ConnectorStore) -> int | str:
        ctx.thread_revoked(self.thread)
        return 0


@dataclass(frozen=True, kw_only=True)
class RevokeVerb(ConnectorVerb):
    help = "Revoke every issued token, code and trusted browser"

    def apply(self, ctx: ConnectorStore) -> int | str:
        ctx.update(lambda grants: DocumentChange.to(replace(
            grants, requests={}, codes={}, access={}, refresh={}, trusted_browsers={}
        )))
        return 0


@boundary_decoder
def main(argv: Sequence[str] | None = None) -> int | str:
    arguments = sys.argv[1:] if argv is None else argv
    return ConnectorVerb.parse(arguments).apply(ConnectorStore.owned())
