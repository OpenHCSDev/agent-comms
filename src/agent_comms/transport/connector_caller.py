"""Who a connector request acts as."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from pydantic import JsonValue

from agent_comms.comms import Comms
from agent_comms.messaging.bus_publication import DirectorOrigin
from agent_comms.messaging.read_basis import OperatorOutcomes
from agent_comms.nominal.field_codec import FieldCodec, MalformedRecord, RootId
from agent_comms.planning.order_words import DirectiveText
from agent_comms.registry.registry_store import RegistryStore
from agent_comms.transport.request import ToolRequest, tool_field
from agent_comms.transport.tool_caller import (
    ForkCaller,
    HumanCaller,
    NoThreadCaller,
    ThreadCaller,
    ToolCaller,
)
from agent_comms.transport.tool_request import ReadTool, ServedRequest, ToolArgumentsRefusedError
from agent_comms.transport.tool_result import ToolResult
from agent_comms_hooks import ToolRefusedError


class GrantRefusedError(ToolRefusedError):
    """The bearer token identifies no live caller authorized for the served root."""


@dataclass(frozen=True, kw_only=True)
class ActingFork:
    fork: Annotated[ForkCaller | NoThreadCaller, *tool_field(
        "Your own name on the bus, as {\"name\": \"...\"}. Several sessions share this grant, "
        "so take one distinct name and pass it on every call: what you send is from that name, "
        "and its inbox holds what is sent to it. Omitted, you act as the shared grant itself."
    )] = NoThreadCaller()


class ConnectorCaller(ToolCaller):
    """The MCP contract, independent of whether its principal is a bus thread."""

    def require_root(self, root: Path) -> None:
        """Existing thread and Director grants keep their current root access."""

    def require_observation(self) -> None:
        """Only a UI grant observes the owner's UI; a thread grant calls tools."""
        raise GrantRefusedError("This grant cannot observe the UI; observation needs a UI grant.")

    def prepare_call(self, comms: Comms, acting: ToolCaller) -> None:
        """A thread-free, unscoped caller has no membership to publish."""

    def exposed(self) -> dict[str, type[ServedRequest] | type[ConnectorTool]]:
        return {tool.declared_name: tool for tool in ServedRequest.members_with(ServedRequest)}

    def acting(self, arguments: Mapping[str, object]) -> tuple[ToolCaller, Mapping[str, object]]:
        return self, arguments

    def input_schema(self, tool: type[ToolRequest]) -> dict[str, JsonValue]:
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

    def exposed(self) -> dict[str, type[ServedRequest] | type[ConnectorTool]]:
        return {tool.declared_name: tool for tool in ServedRequest.members_with(ReadTool)}

    def invoke(self, request: ServedRequest, comms: Comms) -> ToolResult:
        request.require_read_only()
        return super().invoke(request, comms)


@dataclass(frozen=True)
class ScopedUiCaller(ConnectorCaller, NoThreadCaller):
    """A UI caller acts for the owner without a thread, inside one original native
    namespace, whether or not it may write."""

    namespace: RootId

    def require_root(self, root: Path) -> None:
        RegistryStore.at(root).privacy().require_root(
            self.namespace, GrantRefusedError("UI grant belongs to a different root."))

    def require_observation(self) -> None:
        """A UI grant, read-only or writable, observes the root it is scoped to."""

    def prepare_call(self, comms: Comms, acting: ToolCaller) -> None:
        """Check the opened root before argument context reads."""
        self.require_root(comms.root)
        super().prepare_call(comms, acting)

    def invoke(self, request: ServedRequest, comms: Comms) -> ToolResult:
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

    def input_schema(self, tool: type[ToolRequest]) -> dict[str, JsonValue]:
        return FieldCodec.record_schema(tool, ActingFork)


@dataclass(frozen=True)
class DirectorCaller(OperatorOutcomes, ForkingCaller):
    """claude-ai speaks for the owner, so its sends carry the director origin, which records each
    message to a thread as a directive like the owner's own. Each remote Claude session on this
    grant names itself with the fork argument: it is registered as claude-ai's child under its
    own name, so teammates can tell the sessions apart and see who did what."""

    def require_directive(self, comms: Comms, body: str) -> None:
        """A directive passes DirectiveText."""
        DirectiveText.require(body)

    def origin(self) -> DirectorOrigin:
        return DirectorOrigin()

    def exposed(self) -> dict[str, type[ServedRequest] | type[ConnectorTool]]:
        return super().exposed() | {
            tool.declared_name: tool for tool in ConnectorTool.members_with(ConnectorTool)
        }


class ConnectorTool(ServedRequest[DirectorCaller], affix="ConnectorTool"):
    """Connector-only declarations are absent from the native/CLI tool registry."""


CALLER = DirectorCaller("claude-ai")  # Owner-typed calls act as this registered bus thread.
