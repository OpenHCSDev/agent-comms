"""The MCP protocol Core's connector speaks: JSON-RPC envelopes and errors, the message family,
results, progress and streamed events.

Each message answers for itself against ``McpServer``, the server's half of the protocol, so a
client such as ``remote_core`` imports the protocol without importing the server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, ClassVar, Literal, Self

from aiohttp import web
from pydantic import JsonValue

from agent_comms.cli_bridges.cli_protocol import CoreError
from agent_comms.coordination.errors import CommandFailure, Refused
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import (
    FieldCodec,
    NullMember,
    RecordMember,
    SingleFieldValue,
    SubclassUnion,
    WireExclude,
    WireName,
    WireOmitDefault,
    boundary_decoder,
    projected,
)
from agent_comms.transport.connector_caller import ConnectorCaller, ConnectorTool
from agent_comms.transport.progress import (
    CancellableWireExport,
    WireExportCancelled,
    WireExportObserver,
    WireExportProgress,
)
from agent_comms.transport.request import ToolCall, ToolRequest
from agent_comms.transport.tool_request import ServedRequest
from agent_comms_hooks import ToolRefusedError

JsonRpcId = int | str
PROTOCOL_VERSION = "2025-06-18"


def declared(record: type, values: Mapping[str, object]) -> dict[str, object]:
    """The values a record declares; an OAuth peer may send parameters it does not read."""
    names = {plan.key for plan in FieldCodec.field_plans(record)}
    return {key: value for key, value in values.items() if key in names}


class McpServer(ABC):
    """The server's half of the protocol: what a message's answer reads and calls on the
    endpoint it arrived at."""

    observed: dict[JsonRpcId, CancellableWireExport]
    """The running calls a cancel can name, by request id."""
    session: McpSession
    """The session this endpoint serves."""

    @abstractmethod
    def announces(self) -> str:
        """Which bus is behind this endpoint, in the words its own identity gives.

        A client that learns this from a tool call learns it after it has read something,
        and reading the wrong bus is the failure this answers, so the handshake says it.
        """

    @abstractmethod
    async def call(
        self, tool: type[ServedRequest] | type[ConnectorTool],
        arguments: Mapping[str, object], caller: ConnectorCaller, observer: WireExportObserver,
    ) -> AnsweredCall: ...

    @abstractmethod
    def cancel_call(self, request_id: JsonRpcId) -> None: ...


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
    instructions: str = ""

    shared_grant: ClassVar[str] = (
        "Several Claude sessions can share one connector grant. When a tool offers the fork "
        "argument, choose one distinct name for this session and pass {\"name\": \"...\"} on "
        "every call, so teammates can tell you apart: your sends come from that name and your "
        "inbox (comms_inbox with thread set to it) holds only what is sent to it."
    )

    @classmethod
    def announcing(cls, bus: str) -> Self:
        """The handshake a server answers: which bus it is, and then how to share it.

        Which bus comes first because a session holding several connectors reads this as
        the one thing that distinguishes them; everything else here is the same on all of
        them, which was exactly the trouble.
        """
        return cls(instructions=f"{bus}\n\n{cls.shared_grant}")


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
        cls, name: str, tool: type[ToolRequest], caller: ConnectorCaller
    ) -> ToolDescription:
        """The name is the key tools/call dispatches on, so listing and calling share it."""
        return cls(name, tool.label, f"{tool.description} {tool.bus_effect}",
                   caller.input_schema(tool), ToolAnnotations(tool.is_read_only()))


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
                return SessionClaim()
            case self.id:
                return SessionClaim()
            case _:
                return PastSession()


class SessionClaim:
    """What a request's session header says about the process answering it."""

    async def answer(
        self, message: McpMessage, connector: McpServer, caller: ConnectorCaller,
        http: web.Request,
    ) -> web.StreamResponse:
        return await message.answer(connector, caller, http)


class PastSession(SessionClaim):
    """An id from an earlier process: its tool list may be missing what this one serves."""

    async def answer(
        self, message: McpMessage, connector: McpServer, caller: ConnectorCaller,
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
        self, connector: McpServer, caller: ConnectorCaller, http: web.Request,
    ) -> web.StreamResponse: ...

    async def past_session(
        self, connector: McpServer, caller: ConnectorCaller, http: web.Request,
    ) -> web.StreamResponse:
        """The session this message names ended with the process that issued it."""
        return web.Response(status=404)


@dataclass(frozen=True, kw_only=True)
class McpNotification(McpMessage, declared_name=None):
    """A notification gets no reply."""

    params: dict[str, object] = field(default_factory=dict)

    async def answer(
        self, connector: McpServer, caller: ConnectorCaller, http: web.Request,
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
        self, connector: McpServer, caller: ConnectorCaller, http: web.Request,
    ) -> web.StreamResponse:
        connector.cancel_call(FieldCodec.decode(CancelledParams, self.params).request_id)
        return web.Response(status=202)


@dataclass(frozen=True, kw_only=True)
class McpRequest(McpMessage):
    id: JsonRpcId

    async def answer(
        self, connector: McpServer, caller: ConnectorCaller, http: web.Request,
    ) -> web.StreamResponse:
        return web.json_response(
            FieldCodec.encode(McpReply(self.id, await self.result(connector, caller)))
        )

    @abstractmethod
    async def result(self, connector: McpServer, caller: ConnectorCaller) -> McpResult: ...


@dataclass(frozen=True, kw_only=True)
class InitializeMessage(McpRequest, declared_name="initialize"):
    params: dict[str, object]

    async def answer(
        self, connector: McpServer, caller: ConnectorCaller, http: web.Request,
    ) -> web.StreamResponse:
        response = await super().answer(connector, caller, http)
        response.headers[SESSION_HEADER] = connector.session.id
        return response

    async def past_session(
        self, connector: McpServer, caller: ConnectorCaller, http: web.Request,
    ) -> web.StreamResponse:
        """Initializing is how a past session's client starts this process's session."""
        return await self.answer(connector, caller, http)

    async def result(self, connector: McpServer, caller: ConnectorCaller) -> InitializeResult:
        return InitializeResult.announcing(connector.announces())


@dataclass(frozen=True, kw_only=True)
class PingMessage(McpRequest, declared_name="ping"):
    params: dict[str, object] = field(default_factory=dict)

    async def result(self, connector: McpServer, caller: ConnectorCaller) -> McpResult:
        return McpResult()


@dataclass(frozen=True, kw_only=True)
class ToolsListMessage(McpRequest, declared_name="tools/list"):
    params: dict[str, object] = field(default_factory=dict)

    async def result(self, connector: McpServer, caller: ConnectorCaller) -> ToolsListResult:
        return ToolsListResult(tuple(
            ToolDescription.of(name, tool, caller) for name, tool in caller.exposed().items()
        ))


class ProgressRequest(SubclassUnion, affix="Progress"):
    """Whether a tools/call asked for progress: no token (null) or the client's token."""

    @abstractmethod
    async def answered(
        self, message: ToolsCallMessage, connector: McpServer, caller: ConnectorCaller,
        http: web.Request,
    ) -> web.StreamResponse: ...


@dataclass(frozen=True)
class NoProgress(NullMember, ProgressRequest):
    async def answered(
        self, message: ToolsCallMessage, connector: McpServer, caller: ConnectorCaller,
        http: web.Request,
    ) -> web.StreamResponse:
        return web.json_response(FieldCodec.encode(
            ToolsCallReply(message.id, await message.result(connector, caller, WireExportObserver()))))


@dataclass(frozen=True)
class ProgressToken(SingleFieldValue, ProgressRequest):
    """MCP's progressToken is a string or a number; Claude Code sends a number."""

    token: JsonRpcId

    async def answered(
        self, message: ToolsCallMessage, connector: McpServer, caller: ConnectorCaller,
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
        self, connector: McpServer, caller: ConnectorCaller, http: web.Request,
    ) -> web.StreamResponse:
        """The call's own meta says whether the client asked for progress."""
        meta = ToolCallMeta.of(self.params.meta)
        return await meta.progress.answered(self, connector, caller, http)

    async def result(
        self, connector: McpServer, caller: ConnectorCaller, observer: WireExportObserver,
    ) -> ToolsCallResult:
        tool = caller.exposed().get(self.params.name)
        if tool is None:
            return ToolsCallResult.failed(
                ToolRefusedError(f"The connector exposes no tool {self.params.name!r}.")
            )
        try:
            answered = await connector.call(tool, self.params.arguments, caller, observer)
            return answered.result()
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
class AnsweredCall:
    """A tool's declared reply and the new board rows naming the caller who made it."""

    reply: dict[str, JsonValue]
    board: tuple[str, ...]

    def result(self) -> ToolsCallResult:
        """The rows ride as their own text after the reply, never inside it: structured
        content stays exactly the declared result that remote cores decode."""
        return ToolsCallResult((TextContent(json.dumps(self.reply)),
                                *(TextContent(line) for line in self.board)), False, self.reply)


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
            error, = self.result.content
            CoreError.decoded(error.text).raise_error()
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
