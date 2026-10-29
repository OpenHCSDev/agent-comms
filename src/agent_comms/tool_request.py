"""Shared tool request declarations and caller capabilities."""

from __future__ import annotations

import asyncio
from abc import abstractmethod
from collections.abc import Callable, Mapping
from contextvars import copy_context
from dataclasses import dataclass, field
from typing import Annotated, ClassVar, NoReturn

from pydantic import JsonValue

from .child_process import DroppableRead, join_retirement
from .comms import Comms
from .declared_family import DeclaredFamily
from .errors import ToolRefusedError
from .field_codec import (
    ContextSource,
    DefaultNonNull,
    Description,
    FieldCodec,
    FieldDirective,
    MalformedRecord,
    NullMember,
    SingleFieldValue,
    ToolCallId,
    WireChoices,
    WireName,
)
from .native_tools import (
    ArgumentTool,
    CoordinationCall,
    NativeToolDeclaration,
)
from .thread_execution import ThreadCommandAvailability
from .tool_caller import NoThreadCaller, ThreadCaller, ToolCaller
from .tool_declaration import NativeRequestTools
from .tool_result import ToolResult


class ToolArgumentsRefusedError(MalformedRecord, ToolRefusedError):
    """Bad external tool arguments, not malformed stored state or a broken implementation."""


class ContextBinding(DeclaredFamily, affix="Binding"):
    @classmethod
    @abstractmethod
    def resolve(cls, subject: str, actor: str | None) -> str: ...


class SubjectBinding(ContextBinding):
    @classmethod
    def resolve(cls, subject: str, actor: str | None) -> str:
        return subject


def tool_field(
    description: str,
    *,
    wire_name=None,
    binding: type[ContextBinding] | None = None,
    choices=None,
) -> tuple[FieldDirective, ...]:
    directives: list[FieldDirective] = [
        Description(description),
        DefaultNonNull(),
    ]
    if wire_name is not None:
        directives.append(WireName(wire_name))
    if binding is not None:
        directives.append(ContextSource(binding))
    if choices is not None:
        directives.append(WireChoices(choices))
    return tuple(directives)


@dataclass(frozen=True)
class ListedNames(SingleFieldValue):
    """Names a tool call listed for an optional argument."""

    names: tuple[str, ...]


@dataclass(frozen=True)
class OmittedNames(NullMember):
    """A tool call left an optional list of names out, or sent null."""

    names: ClassVar[tuple[str, ...]] = ()


@dataclass(frozen=True)
class ToolCall:
    """The MCP tools/call parameters: a tool by the name the connector exposes it under."""

    name: str
    arguments: dict[str, object] = field(default_factory=dict)
    meta: Annotated[dict[str, object], WireName("_meta")] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class ToolRequest[Caller: ToolCaller = ToolCaller](
    ArgumentTool, ThreadCommandAvailability, NativeRequestTools, NativeToolDeclaration
):
    @classmethod
    def create_call(
        cls, call_id: ToolCallId, arguments: dict[str, object]
    ) -> CoordinationCall:
        return CoordinationCall(
            call_id=call_id, declared_name=cls.declared_name, arguments=arguments)

    description: ClassVar[str]
    read_only_hint: ClassVar[bool] = False
    bus_effect: ClassVar[str] = (
        "Changes the bus. Tristan has authorized claude-ai to call this without asking him."
    )
    context: ClassVar[str | None] = None
    action_label: ClassVar[str | None] = None

    @classmethod
    async def run_async[Result](cls, operation: Callable[[], Result]) -> Result:
        """A bus-changing call retires its work even when its transport goes away."""
        return await join_retirement(asyncio.create_task(asyncio.to_thread(operation)))

    @classmethod
    def cancel_on_shutdown(cls, pending: asyncio.Future) -> None:
        """An accepted mutation must finish, even if its worker has not started yet."""

    @classmethod
    def context_bindings(cls) -> dict[str, type[ContextBinding]]:
        return {
            plan.key: binding.value
            for plan in FieldCodec.field_plans(cls)
            for binding in ContextSource.single(plan.annotation)
        }

    @classmethod
    def called(cls) -> ToolCall:
        """A connector call of this tool; the connector exposes it under its declared name."""
        return ToolCall(cls.declared_name)

    @classmethod
    def schema(cls) -> dict[str, object]:
        result = {
            "name": cls.declared_name,
            "label": cls.label,
            "description": cls.description,
            "parameters": FieldCodec.record_schema(cls),
        }
        if cls.context is not None:
            result.update(
                context=cls.context,
                action_label=cls.action_label or cls.label,
                action_order=cls.declared_position(),
                context_bindings={
                    key: binding.declared_name for key, binding in cls.context_bindings().items()
                },
            )
        return result


    @classmethod
    def invoke(
        cls, comms: Comms, arguments: Mapping[str, object], *, caller: ToolCaller
    ) -> dict[str, JsonValue]:
        """The reply every adapter receives: the encoding of the tool's declared result."""
        if cls.family_discriminator in arguments:
            raise ToolArgumentsRefusedError(f"Unknown tool argument: {cls.family_discriminator}")
        try:
            request = FieldCodec.decode_record(
                cls, dict(arguments), field_types=cls.context_types(comms, arguments))
        except MalformedRecord as error:
            raise ToolArgumentsRefusedError(
                f"Invalid {cls.declared_name} arguments: {error}"
            ) from error
        return request.reply(FieldCodec.encode(caller.invoke(request, comms).to_wire()), caller)

    def reply(self, read: dict[str, JsonValue], caller: ToolCaller) -> dict[str, JsonValue]:
        """The encoded result as every adapter receives it; a tool whose reply must name
        what it read overrides this."""
        return read

    @classmethod
    def context_types(cls, comms: Comms, arguments: Mapping[str, object]) -> Mapping[str, type[object]]:
        """Ordinary tools need no contextual narrowing of their declared field types."""
        return {}

    @abstractmethod
    def apply(self, comms: Comms, caller: Caller) -> ToolResult:
        """Each tool declares its own result record; invoke alone encodes it."""

    def apply_without_thread(
        self: ToolRequest[ToolCaller], comms: Comms, caller: NoThreadCaller
    ) -> ToolResult:
        return self.apply(comms, caller)


class PublishingTool[Caller: ToolCaller = ToolCaller]:
    """A publication refuses at its store's write guard during maintenance; waiting here
    would hold the connector's stop."""

    def apply(self, comms: Comms, caller: Caller) -> ToolResult:
        return self.publish(comms, caller)

    @abstractmethod
    def publish(self, comms: Comms, caller: Caller) -> ToolResult: ...


class PublishingToolRequest[Caller: ToolCaller = ToolCaller](
    PublishingTool[Caller], ToolRequest[Caller], declared_name=None,
):
    """Native declarations using the shared publication capability."""


class LiveOwnerTool:
    """A tool whose reply needs a launched native owner or a running restart watcher."""

    __slots__ = ()


class ReadTool:
    """A tool whose apply changes nothing on the bus."""

    __slots__ = ()
    bus_effect: ClassVar[str] = "Reads only; changes nothing on the bus."
    read_only_hint: ClassVar[bool] = True

    @classmethod
    async def run_async[Result](cls, operation: Callable[[], Result]) -> Result:
        """No bus mutation is abandoned when this reader leaves with the transport."""
        context = copy_context()
        return await DroppableRead(lambda: context.run(operation)).answer()

    @classmethod
    def cancel_on_shutdown(cls, pending: asyncio.Future) -> None:
        pending.cancel()


@dataclass(frozen=True, kw_only=True)
class ThreadReadTool(ReadTool, ToolRequest):
    thread: Annotated[str, *tool_field("Thread to read; omit to read your own")] = (
        ""
    )


class ThreadToolRequest(ToolRequest[ThreadCaller]):
    """Self-relative requests declare their required caller at the shared boundary."""

    def apply_without_thread(self, comms: Comms, caller: NoThreadCaller) -> NoReturn:
        return caller.refuse_thread()
