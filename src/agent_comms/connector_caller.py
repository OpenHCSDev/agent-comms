"""Who a connector request acts as."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from agent_comms_hooks import ToolRefusedError
from pydantic import JsonValue

from .bus_publication import DirectorOrigin
from .comms import Comms
from .nominal.field_codec import FieldCodec, MalformedRecord, RootId
from .registry_store import RegistryStore
from .todos import DirectiveText
from .read_basis import OperatorOutcomes
from .tool_caller import ForkCaller, HumanCaller, NoThreadCaller, ThreadCaller, ToolCaller
from .tool_request import ReadTool, ToolArgumentsRefusedError, ToolRequest
from .tool_result import ToolResult


class GrantRefusedError(ToolRefusedError):
    """The bearer token identifies no live caller authorized for the served root."""


@dataclass(frozen=True, kw_only=True)
class ActingFork:
    fork: ForkCaller | NoThreadCaller = NoThreadCaller()


class ConnectorCaller(ToolCaller):
    """The MCP contract, independent of whether its principal is a bus thread."""

    def require_root(self, root: Path) -> None:
        """Existing thread and Director grants keep their current root access."""

    def prepare_call(self, comms: Comms, acting: ToolCaller) -> None:
        """A thread-free, unscoped caller has no membership to publish."""

    def exposed(self) -> dict[str, type[ToolRequest] | type[ConnectorTool]]:
        return {tool.declared_name: tool for tool in ToolRequest.members_with(ToolRequest)}

    def acting(self, arguments: Mapping[str, object]) -> tuple[ToolCaller, Mapping[str, object]]:
        return self, arguments

    def input_schema(self, tool: type[ToolRequest] | type[ConnectorTool]) -> dict[str, JsonValue]:
        return FieldCodec.record_schema(tool)


@dataclass(frozen=True)
class ThreadConnectorCaller(ConnectorCaller, ThreadCaller):
    """A registered thread calling through MCP with its own grant."""

    def prepare_call(self, comms: Comms, acting: ToolCaller) -> None:
        self.identity(comms)
        acting.join(comms, self.own_thread())


@dataclass(frozen=True)
class ReadOnlyConnectorCaller(ConnectorCaller, NoThreadCaller):
    """A thread-free MCP caller with the declared read capability."""

    def exposed(self) -> dict[str, type[ToolRequest] | type[ConnectorTool]]:
        return {tool.declared_name: tool for tool in ToolRequest.members_with(ReadTool)}

    def invoke(self, request: ToolRequest, comms: Comms) -> ToolResult:
        request.require_read_only()
        return super().invoke(request, comms)


@dataclass(frozen=True)
class ScopedUiCaller(ConnectorCaller):
    """UI authority belongs to one original native namespace, independently of writes."""

    namespace: RootId

    def require_root(self, root: Path) -> None:
        RegistryStore.at(root).privacy().require_root(
            self.namespace, GrantRefusedError("UI grant belongs to a different root."))

    def prepare_call(self, comms: Comms, acting: ToolCaller) -> None:
        """Check the opened root before argument context reads."""
        self.require_root(comms.root)
        super().prepare_call(comms, acting)

    def invoke(self, request: ToolRequest, comms: Comms) -> ToolResult:
        """Direct and connector invocation use the same namespace guard."""
        self.require_root(comms.root)
        return super().invoke(request, comms)


@dataclass(frozen=True)
class UiReadCaller(ScopedUiCaller, ReadOnlyConnectorCaller):
    """The provisioned reader retains its declared read-only capability."""


@dataclass(frozen=True)
class UiCaller(ScopedUiCaller, HumanCaller):
    """The owner's writable UI, without an executable colleague's identity."""


class ForkingCaller(ThreadConnectorCaller):
    """A thread grant; its holder may act as one of its own registered teammate forks."""

    def acting(self, arguments: Mapping[str, object]) -> tuple[ToolCaller, Mapping[str, object]]:
        keys = {plan.key for plan in FieldCodec.field_plans(ActingFork)}
        selected = {key: value for key, value in arguments.items() if key in keys}
        rest = {key: value for key, value in arguments.items() if key not in keys}
        try:
            acting_fork = FieldCodec.decode(ActingFork, selected)
        except MalformedRecord as error:
            raise ToolArgumentsRefusedError(
                f"Invalid connector caller arguments: {error}"
            ) from error
        return acting_fork.fork.acting_caller(self), rest

    def input_schema(self, tool: type[ToolRequest] | type[ConnectorTool]) -> dict[str, JsonValue]:
        return FieldCodec.record_schema(tool, ActingFork)


@dataclass(frozen=True)
class DirectorCaller(OperatorOutcomes, ThreadConnectorCaller):
    """claude-ai speaks for the owner, so its sends carry the director origin, which records each
    message to a thread as a directive like the owner's own."""

    def require_directive(self, comms: Comms, body: str) -> None:
        """A directive passes DirectiveText."""
        DirectiveText.require(body)

    def origin(self) -> DirectorOrigin:
        return DirectorOrigin()

    def exposed(self) -> dict[str, type[ToolRequest] | type[ConnectorTool]]:
        return super().exposed() | {
            tool.declared_name: tool for tool in ConnectorTool.members_with(ConnectorTool)
        }


class ConnectorTool(ToolRequest[DirectorCaller], affix="ConnectorTool"):
    """Connector-only declarations are absent from the native/CLI tool registry."""


CALLER = DirectorCaller("claude-ai")  # Owner-typed calls act as this registered bus thread.
