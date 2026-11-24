"""Client-safe MCP JSON-RPC wire types, results, progress, and streamed replies.

Kept separate from `agent_comms.transport.mcp` (server dispatch, `aiohttp.web`,
`ConnectorCaller`, `ServedRequest`) so remote UI clients (`remote_core`, Toad Qt/Android)
speak MCP without importing Core's server stack.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Annotated, ClassVar, Literal, Self

from pydantic import JsonValue

from agent_comms.cli_bridges.cli_protocol import CoreError
from agent_comms.nominal.field_codec import (
    FieldCodec,
    RecordMember,
    WireName,
    WireOmitDefault,
    boundary_decoder,
)
from agent_comms.transport.progress import WireExportObserver, WireExportProgress
from agent_comms.transport.request import ToolCall, ToolRequest

JsonRpcId = int | str
PROTOCOL_VERSION = "2025-06-18"


def declared(record: type, values: Mapping[str, object]) -> dict[str, object]:
    """The values a record declares; an OAuth peer may send parameters it does not read."""
    names = {plan.key for plan in FieldCodec.field_plans(record)}
    return {key: value for key, value in values.items() if key in names}


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
    protocol_version: Annotated[str, WireName("protocolVersion")] = PROTOCOL_VERSION
    capabilities: ServerCapabilities = ServerCapabilities()
    server_info: Annotated[ServerInfo, WireName("serverInfo")] = ServerInfo()
    instructions: str = ""

    shared_grant: ClassVar[str] = (
        "Several sessions can share one connector grant. When a tool offers the fork "
        "argument, choose one distinct name for this session and pass {\"name\": \"...\"} on "
        "every call, so teammates can tell you apart: your sends come from that name and your "
        "inbox (comms_inbox with thread set to it) holds only what is sent to it. To take over "
        "an existing thread while keeping its identity, switch it with comms_cli_inbox if it "
        "runs on Native Pi, then pass that thread's name in fork."
    )

    @classmethod
    def announcing(
        cls,
        bus: str,
        listed: Iterable[type[ToolRequest]] = (),
        server_info: ServerInfo = ServerInfo(),
    ) -> Self:
        """The handshake a server answers: which bus it is, how to share it, and then what
        each listed tool has to say before its first call, so a client is never told to
        call a tool its list does not name.

        Which bus comes first because a session holding several connectors reads this as
        the one thing that distinguishes them; everything else here is the same on all of
        them, which was exactly the trouble.
        """
        said = dict.fromkeys(tool.handshake for tool in listed if tool.handshake)
        return cls(
            server_info=server_info,
            instructions="\n\n".join((bus, cls.shared_grant, *said)),
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


@dataclass(frozen=True)
class ToolsListResult(McpResult):
    tools: tuple[ToolDescription, ...]


@dataclass(frozen=True)
class CancelledParams:
    request_id: Annotated[JsonRpcId, WireName("requestId")]


@dataclass(frozen=True, kw_only=True)
class CancelledFields:
    params: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class CancelledMessage(McpEnvelope, CancelledFields):
    """The client stops one of its running calls; a call that cannot stop finishes as usual."""

    method: Literal["notifications/cancelled"] = "notifications/cancelled"


@dataclass(frozen=True, kw_only=True)
class ToolsListFields:
    id: JsonRpcId
    params: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class ToolsListMessage(McpEnvelope, ToolsListFields):
    method: Literal["tools/list"] = "tools/list"


@dataclass(frozen=True, kw_only=True)
class ToolsCallFields:
    id: JsonRpcId
    params: ToolCall


@dataclass(frozen=True, kw_only=True)
class ToolsCallMessage(McpEnvelope, ToolsCallFields):
    method: Literal["tools/call"] = "tools/call"


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
        notice = cls(
            "2.0",
            "notifications/progress",
            ProgressParams(token, progress.messages_written, ExportProgressMeta(progress)),
        )
        return cls.framed(FieldCodec.encode(notice))


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
        return ToolsCallResult(
            (TextContent(json.dumps(self.reply)), *(TextContent(line) for line in self.board)),
            False,
            self.reply,
        )


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
class ToolsListReply(McpReply):
    """The tools/list answer: the tools this connector exposes to the caller who asked."""

    result: ToolsListResult


@dataclass(frozen=True)
class ToolsCallReply(McpReply, StreamedEvent):
    result: ToolsCallResult

    def structured(self) -> dict[str, JsonValue]:
        """The tool's declared reply, or its declared failure raised."""
        if self.result.is_error:
            (error,) = self.result.content
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
            event = FieldCodec.decode(
                ProgressNotice | ToolsCallReply, json.loads(text[len("data: ") :])
            )
            event.delivered(observer, replies)
