"""Shared tool request declarations and caller capabilities."""

from __future__ import annotations


from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import get_original_bases
from typing import Self, Annotated, ClassVar, NoReturn, TypeVar, cast, get_args, get_origin

from agent_comms_hooks import ToolRefusedError
from pydantic import JsonValue

from .child_process import AsyncWork, ReadWork
from .comms import Comms
from .nominal.declared_family import DeclaredFamily
from .exporting import WireExportObserver
from .nominal.field_codec import (
    WireTransient,
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
from .storage.maintenance_barrier import FreshIngress
from .native_tools import (
    ArgumentTool,
    CoordinationCall,
    NativeToolDeclaration,
)
from .threads import ThreadCommandAvailability
from .tool_caller import NoThreadCaller, ThreadCaller, ToolCaller
from .tool_declaration import NativeRequestTools, ToolDeclaration
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
class ConnectorService(ABC):
    """What a tool can ask of the connector serving it; only a connector call has one."""

    @abstractmethod
    def watch_url(self, thread: str) -> ToolResult: ...


class NoConnectorService(ConnectorService):
    def watch_url(self, thread: str) -> ToolResult:
        raise ToolRefusedError("Only a connector call can open an inbox watch.")


class ToolRequest[Caller: ToolCaller = ToolCaller, Result: ToolResult = ToolResult](
    AsyncWork, FreshIngress, ArgumentTool, ThreadCommandAvailability, ToolDeclaration
):
    """A transport request; native exposure is an independent declared capability."""

    description: ClassVar[str]
    read_only_hint: ClassVar[bool] = False
    bus_effect: ClassVar[str] = (
        "Changes the bus. Tristan has authorized claude-ai to call this without asking him."
    )
    @classmethod
    def context_bindings(cls) -> dict[str, type[ContextBinding]]:
        return {
            plan.key: binding.value
            for plan in FieldCodec.field_plans(cls)
            for binding in ContextSource.single(plan.annotation)
        }

    def called(self) -> ToolCall:
        """This request as a connector call: its declared name and the arguments invoke
        decodes back into it."""
        return ToolCall(self.declared_name, self.arguments)

    @classmethod
    def schema(cls) -> dict[str, object]:
        result = {
            "name": cls.declared_name,
            "label": cls.label,
            "description": cls.description,
            "parameters": FieldCodec.record_schema(cls),
        }
        return result

    @classmethod
    def result_type(cls) -> type[Result]:
        """The result this request declares once, as the family's second type argument in its
        own or an inherited class header, followed through every intermediate generic parent;
        a remote reply decodes into it.

        A header that omits the argument still reads as the family default (ToolResult), so
        every path's binding is collected and the most derived wins: a result declared on one
        parent refines the default another parent inherited. Unrelated declarations conflict."""
        bindings: dict[object, list[object]] = {}
        requests = cls.__mro__[:cls.__mro__.index(ToolRequest) + 1]
        for declaration in cls.__mro__:
            generic = (base for base in get_original_bases(declaration) if get_origin(base) in requests)
            for base in generic:
                for parameter, argument in zip(get_origin(base).__type_params__, get_args(base)):
                    bindings.setdefault(parameter, []).append(argument)

        def declared_by(parameter: object, seen: frozenset[object]) -> list[type]:
            found: list[type] = []
            for argument in bindings.get(parameter, ()):
                if isinstance(argument, TypeVar):
                    if argument not in seen:
                        found.extend(declared_by(argument, seen | {argument}))
                else:
                    found.append(argument)
            return found

        declared = ToolRequest.__type_params__[1]
        default = declared.__default__
        candidates = list(dict.fromkeys(
            found for found in declared_by(declared, frozenset({declared})) if found is not default
        )) or [default]
        if len(candidates) == 1:
            return cast(type[Result], candidates[0])
        for result in candidates:
            if isinstance(result, type) and all(
                    isinstance(other, type) and issubclass(result, other) for other in candidates):
                return cast(type[Result], result)
        raise TypeError(f"{cls.__name__} declares unrelated results: {candidates}")

    @classmethod
    def invoke(
        cls, comms: Comms, arguments: Mapping[str, object], *, caller: ToolCaller
    ) -> dict[str, JsonValue]:
        """The reply every adapter receives: the encoding of the tool's declared result."""
        return cls.decoded(comms, arguments).replied(comms, caller)

    @classmethod
    def decoded(cls, comms: Comms, arguments: Mapping[str, object]) -> Self:
        """This tool's request as the wire's arguments declare it."""
        if cls.family_discriminator in arguments:
            raise ToolArgumentsRefusedError(f"Unknown tool argument: {cls.family_discriminator}")
        try:
            return FieldCodec.decode_record(
                cls, dict(arguments), field_types=cls.context_types(comms, arguments))
        except MalformedRecord as error:
            raise ToolArgumentsRefusedError(
                f"Invalid {cls.declared_name} arguments: {error}"
            ) from error

    def bound(self, observer: WireExportObserver) -> Self:
        """A request that reports no progress runs the same whatever observes it."""
        return self

    def served_by(self, service: ConnectorService) -> Self:
        """A request that asks nothing of its connector runs the same under any of them."""
        return self

    def posted(self, remote: RequestPoster) -> dict[str, JsonValue]:
        """How a remote access sends this request: one posted call and its reply."""
        return remote.post(self)

    def replied(self, comms: Comms, caller: ToolCaller) -> dict[str, JsonValue]:
        # A request decoded from the wire meets whichever caller arrived; the caller's own
        # invoke checks at runtime that it may apply this request (thread-only refusals).
        applied = caller.invoke(cast(ToolRequest, self), comms)
        return self.reply(FieldCodec.encode(applied.to_wire()), caller)

    def board(self, comms: Comms, caller: ToolCaller) -> tuple[str, ...]:
        """The new board rows naming the caller of this verb, carried beside its reply: the
        reply itself stays the tool's declared result, which remote readers decode."""
        return caller.board(comms)

    def require_read_only(self) -> None:
        """Mutating declarations cannot run under a read-only connector principal."""
        raise ToolRefusedError("This connector grant permits declared reads only.")

    def reply(self, read: dict[str, JsonValue], caller: ToolCaller) -> dict[str, JsonValue]:
        """The encoded result as every adapter receives it; a tool whose reply must name
        what it read overrides this."""
        return read

    @classmethod
    def context_types(cls, comms: Comms, arguments: Mapping[str, object]) -> Mapping[str, type[object]]:
        """Ordinary tools need no contextual narrowing of their declared field types."""
        return {}

    @abstractmethod
    def apply(self, comms: Comms, caller: Caller) -> Result:
        """Each tool declares its own result record; invoke alone encodes it."""

    def apply_without_thread[R: ToolResult](
        self: ToolRequest[ToolCaller, R], comms: Comms, caller: NoThreadCaller
    ) -> R:
        return self.apply(comms, caller)


@dataclass(frozen=True, kw_only=True)
class ObservedToolRequest[Caller: ToolCaller = ToolCaller, Result: ToolResult = ToolResult](
    ToolRequest[Caller, Result], declared_name=None,
):
    """A tool whose run reports progress to an observer and stops when it is cancelled. The
    observer never crosses the wire: each end binds its own, and a remote call streams the
    progress between them and carries a cancel back."""

    observer: Annotated[WireExportObserver, WireTransient()] = field(
        default_factory=WireExportObserver, compare=False)

    def observed_by(self, observer: WireExportObserver) -> Self:
        return replace(self, observer=observer)

    def bound(self, observer: WireExportObserver) -> Self:
        return self.observed_by(observer)

    def posted(self, remote: RequestPoster) -> dict[str, JsonValue]:
        return remote.post_observed(self)


class RequestPoster(ABC):
    """Who sends a request to a Core across the connector; each request picks which way it is
    sent, plainly or with its progress and cancel streamed."""

    @abstractmethod
    def post[C: ToolCaller, R: ToolResult](self, request: ToolRequest[C, R]) -> dict[str, JsonValue]: ...

    @abstractmethod
    def post_observed[C: ToolCaller, R: ToolResult](
        self, request: ObservedToolRequest[C, R],
    ) -> dict[str, JsonValue]: ...


class HumanToolRequest[Result: ToolResult = ToolResult](
    ToolRequest[ToolCaller, Result], declared_name=None,
):
    """A request only the owner's UI may make: Core checks the human caller here, once,
    and the member answers from comms alone."""

    def apply(self, comms: Comms, caller: ToolCaller) -> Result:
        caller.require_human()
        return self.answered(comms)

    @abstractmethod
    def answered(self, comms: Comms) -> Result: ...


class NativeToolRequest[Caller: ToolCaller = ToolCaller, Result: ToolResult = ToolResult](
    ToolRequest[Caller, Result], NativeRequestTools, NativeToolDeclaration, declared_name=None,
):
    """A model command supplies both its native schema and its executing call."""

    native_call: ClassVar[type[CoordinationCall]] = CoordinationCall

    @classmethod
    def schema(cls) -> dict[str, object]:
        return {**super().schema(), "loads_tools": cls.loads_tools}

    @classmethod
    def create_call(
        cls, call_id: ToolCallId, arguments: dict[str, object]
    ) -> CoordinationCall:
        return cls.native_call(
            call_id=call_id, declared_name=cls.declared_name, arguments=arguments)


class PublishingTool[Caller: ToolCaller = ToolCaller, Result: ToolResult = ToolResult]:
    """A publication refuses at its store's write guard during maintenance; waiting here
    would hold the connector's stop."""

    def apply(self, comms: Comms, caller: Caller) -> Result:
        return self.publish(comms, caller)

    @abstractmethod
    def publish(self, comms: Comms, caller: Caller) -> Result: ...


class PublishingToolRequest[Caller: ToolCaller = ToolCaller, Result: ToolResult = ToolResult](
    PublishingTool[Caller, Result], ToolRequest[Caller, Result], declared_name=None,
):
    """Requests using the shared publication capability. Each binds the result its publication
    returns once, here in its base list; a remote caller decodes the reply into it."""


class LiveOwnerTool:
    """A tool whose reply needs a launched native owner or a running restart watcher."""

    __slots__ = ()


class ReadTool(ReadWork, FreshIngress):
    """A tool whose apply changes nothing on the bus."""

    __slots__ = ()
    bus_effect: ClassVar[str] = "Reads only; changes nothing on the bus."
    read_only_hint: ClassVar[bool] = True

    def require_read_only(self) -> None:
        """The declared read capability controls access, independently of MCP hints."""

    def board(self, comms: Comms, caller: ToolCaller) -> tuple[str, ...]:
        """A read is not a verb: it carries what it read, and the board stays in comms_board."""
        return ()

    @classmethod
    def require_entry_open(cls, root: Path) -> None:
        """Declared reads remain available to the installer's readiness checks."""


class CoreRead[Result: ToolResult = ToolResult](ReadTool, ToolRequest[ToolCaller, Result], declared_name=None):
    """A read any caller may ask of Core, answering its declared result and changing nothing."""


@dataclass(frozen=True, kw_only=True)
class ThreadReadTool(ReadTool, NativeToolRequest):
    thread: Annotated[str, *tool_field("Thread to read; omit to read your own")] = (
        ""
    )


class ThreadToolRequest(NativeToolRequest[ThreadCaller]):
    """Self-relative requests declare their required caller at the shared boundary."""

    def apply_without_thread(self, comms: Comms, caller: NoThreadCaller) -> NoReturn:
        return caller.refuse_thread()


class ThreadContextAction[Caller: ToolCaller = ToolCaller](
    NativeToolRequest[Caller], declared_name=None,
):
    """A tool the UI offers as an action in a thread's context menu, listed in its declared
    order with the arguments the selected thread binds."""

    context: ClassVar[str] = "thread"
    action_label: ClassVar[str]

    @classmethod
    def schema(cls) -> dict[str, object]:
        result = super().schema()
        result.update(
            context=cls.context,
            action_label=cls.action_label,
            action_order=cls.declared_position(),
            context_bindings={
                key: binding.declared_name
                for key, binding in cls.context_bindings().items()
            },
        )
        return result
