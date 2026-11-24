"""Server-side MCP JSON-RPC dispatch over `agent_comms.transport.mcp_wire`.

Client-only callers (such as `remote_core` and Toad Qt/Android) import the wire records
directly from `agent_comms.transport.mcp_wire` so they never pull `aiohttp.web`,
`ConnectorCaller`, `ServedRequest`, or `agent_comms.comms`.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, ClassVar, Literal, Self

from aiohttp import web

from agent_comms.coordination.errors import CommandFailure, Refused
from agent_comms.errors import UnexposedToolError
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import (
    FieldCodec,
    NullMember,
    RecordMember,
    SingleFieldValue,
    SubclassUnion,
    WireExclude,
    WireName,
    projected,
)
from agent_comms.transport.connector_caller import ConnectorCaller, ConnectorTool
from agent_comms.transport.mcp_wire import (
    AnsweredCall,
    CancelledFields,
    CancelledParams,
    InitializeResult,
    JsonRpcId,
    McpEnvelope,
    McpReply,
    McpResult,
    ProgressNotice,
    ServerInfo,
    StreamedEvent,
    ToolAnnotations,
    ToolDescription,
    ToolsCallFields,
    ToolsCallReply,
    ToolsCallResult,
    ToolsListFields,
    ToolsListResult,
)
from agent_comms.transport.progress import (
    CancellableWireExport,
    WireExportCancelled,
    WireExportObserver,
    WireExportProgress,
)
from agent_comms.transport.request import ToolRequest
from agent_comms.transport.tool_request import ServedRequest


class McpServer(ABC):
    """The server's half of the protocol: what a message's answer reads and calls on the
    endpoint it arrived at."""

    observed: dict[JsonRpcId, CancellableWireExport]
    """The running calls a cancel can name, by request id."""

    @abstractmethod
    def announces(self) -> str:
        """Which bus is behind this endpoint, in the words its own identity gives.

        A client that learns this from a tool call learns it after it has read something,
        and reading the wrong bus is the failure this answers, so the handshake says it.
        """

    def server_info(self) -> ServerInfo:
        """The MCP serverInfo identifying this endpoint's bus to multi-connector clients."""
        return ServerInfo()

    @abstractmethod
    async def call(
        self,
        tool: type[ServedRequest] | type[ConnectorTool],
        arguments: Mapping[str, object],
        caller: ConnectorCaller,
        observer: WireExportObserver,
    ) -> AnsweredCall: ...

    @abstractmethod
    def cancel_call(self, request_id: JsonRpcId) -> None: ...


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


def tool_description_of(
    name: str, tool: type[ToolRequest], caller: ConnectorCaller
) -> ToolDescription:
    """The name is the key tools/call dispatches on, so listing and calling share it."""
    return ToolDescription(
        name,
        tool.label,
        tool.listed_description(caller),
        caller.input_schema(tool),
        ToolAnnotations(tool.is_read_only()),
    )


@dataclass(frozen=True, kw_only=True)
class McpMessage(DeclaredFamily, affix="Message"):
    """A JSON-RPC message served by the connector; each method answers for itself."""

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


@dataclass(frozen=True, kw_only=True)
class RootsListChangedMessage(McpNotification, declared_name="notifications/roots/list_changed"):
    pass


@dataclass(frozen=True, kw_only=True)
class ServedCancelledMessage(McpNotification, CancelledFields, declared_name="notifications/cancelled"):
    """Serving CancelledMessage."""

    async def answer(
        self, connector: McpServer, caller: ConnectorCaller, http: web.Request,
    ) -> web.StreamResponse:
        connector.cancel_call(FieldCodec.decode(CancelledParams, self.params).request_id)
        return web.Response(status=202)


@dataclass(frozen=True, kw_only=True)
class McpRequest(McpMessage, declared_name=None):
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

    async def result(self, connector: McpServer, caller: ConnectorCaller) -> InitializeResult:
        return InitializeResult.announcing(
            connector.announces(), caller.listed().values(), connector.server_info(),
        )


@dataclass(frozen=True, kw_only=True)
class PingMessage(McpRequest, declared_name="ping"):
    params: dict[str, object] = field(default_factory=dict)

    async def result(self, connector: McpServer, caller: ConnectorCaller) -> McpResult:
        return McpResult()


@dataclass(frozen=True, kw_only=True)
class ServedToolsListMessage(McpRequest, ToolsListFields, declared_name="tools/list"):
    """Serving ToolsListMessage."""

    async def result(self, connector: McpServer, caller: ConnectorCaller) -> ToolsListResult:
        return ToolsListResult(
            tuple(
                tool_description_of(name, tool, caller)
                for name, tool in caller.listed().items()
            )
        )


class ProgressRequest(SubclassUnion, affix="Progress"):
    """Whether a tools/call asked for progress: no token (null) or the client's token."""

    @abstractmethod
    async def answered(
        self,
        message: ServedToolsCallMessage,
        connector: McpServer,
        caller: ConnectorCaller,
        http: web.Request,
    ) -> web.StreamResponse: ...


@dataclass(frozen=True)
class NoProgress(NullMember, ProgressRequest):
    async def answered(
        self,
        message: ServedToolsCallMessage,
        connector: McpServer,
        caller: ConnectorCaller,
        http: web.Request,
    ) -> web.StreamResponse:
        return web.json_response(
            FieldCodec.encode(
                ToolsCallReply(
                    message.id,
                    await message.result(connector, caller, WireExportObserver()),
                )
            )
        )


@dataclass(frozen=True)
class ProgressToken(SingleFieldValue, ProgressRequest):
    """MCP's progressToken is a string or a number; Claude Code sends a number."""

    token: JsonRpcId

    async def answered(
        self,
        message: ServedToolsCallMessage,
        connector: McpServer,
        caller: ConnectorCaller,
        http: web.Request,
    ) -> web.StreamResponse:
        """Stream progress events while the call runs, then its reply, as server-sent events."""
        stream = web.StreamResponse(
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
        )
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
            await stream.write(
                StreamedEvent.framed(FieldCodec.encode(ToolsCallReply(message.id, call.result())))
            )
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
        declared_keys = {entry.key for entry in FieldCodec.argument_fields(cls)}
        return FieldCodec.decode(cls, {key: meta[key] for key in declared_keys & meta.keys()})


@dataclass(frozen=True, kw_only=True)
class ServedToolsCallMessage(McpMessage, ToolsCallFields, declared_name="tools/call"):
    """Serving ToolsCallMessage."""

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
                UnexposedToolError(f"The connector exposes no tool {self.params.name!r}.")
            )
        try:
            tool, arguments = tool.reached(self.params.arguments, caller)
            answered = await connector.call(tool, arguments, caller, observer)
            return answered.result()
        except (
            # Invalid input (ToolRefusedError, MalformedRecord, an unrecorded time) and any
            # declared CommandFailure (git, Refused, coordination) are the caller's refusal;
            # escaping here answers HTTP 500, which the client sees as a lost connection.
            ValueError,
            CommandFailure,
            ConnectionError,
            TimeoutError,
            WireExportCancelled,
        ) as error:
            logging.exception("Connector tool %s failed", self.params.name)
            return ToolsCallResult.failed(error)
