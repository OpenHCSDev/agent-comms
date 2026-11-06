"""Shared tool request declarations and caller capabilities."""

from __future__ import annotations


from abc import ABC, abstractmethod
from annotationlib import Format, ForwardRef, get_annotations
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
from .transport.progress import WireExportObserver
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
from .native_tools import CoordinationCall, NativeToolDeclaration
from .threads import ThreadCommandAvailability
from .tool_caller import NoThreadCaller, ThreadCaller, ToolCaller
from .tool_declaration import NativeRequestTools, ToolDeclaration
from .tool_result import ToolResult
from .transport.request import (
    ConnectionDropped,
    ContextBinding,
    ListedNames,
    ObservedRequest,
    OmittedNames,
    RequestPoster,
    SubjectBinding,
    ToolCall,
    declared_result,
    ToolRequest,
    tool_field,
)


class ToolArgumentsRefusedError(MalformedRecord, ToolRefusedError):
    """Bad external tool arguments, not malformed stored state or a broken implementation."""


@dataclass(frozen=True, kw_only=True)
class ConnectorService(ABC):
    """What a tool can ask of the connector serving it; only a connector call has one."""

    @abstractmethod
    def watch_url(self, thread: str) -> ToolResult: ...


class NoConnectorService(ConnectorService):
    def watch_url(self, thread: str) -> ToolResult:
        raise ToolRefusedError("Only a connector call can open an inbox watch.")


class ServedRequest[Caller: ToolCaller = ToolCaller, Result: ToolResult = ToolResult](
    AsyncWork, ThreadCommandAvailability, ToolRequest[Result], declared_name=None,
):
    """A tool request as the Core that runs it sees it: the declaration plus decoding a call,
    applying it under a caller and replying. Only the server imports this."""

    @classmethod
    def result_type(cls) -> type[Result]:
        """What this request's apply returns, while a member still declares its result there.
        A generic apply names its owner's own type parameter (OwnerScopedRead's Result, not
        necessarily ToolRequest's), so that parameter is the one followed through the headers."""
        apply = cls.apply
        owner = next(declaration for declaration in cls.__mro__ if "apply" in vars(declaration))
        annotation = get_annotations(apply, format=Format.STRING)["return"]
        parameters = {parameter.__name__: parameter for parameter in owner.__type_params__}
        if annotation in parameters:
            return cast(type[Result], declared_result(cls, parameters[annotation]))
        return cast(type[Result], ForwardRef(annotation, module=apply.__module__).evaluate())

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

    def replied(self, comms: Comms, caller: ToolCaller) -> dict[str, JsonValue]:
        # A request decoded from the wire meets whichever caller arrived; the caller's own
        # invoke checks at runtime that it may apply this request (thread-only refusals).
        applied = caller.invoke(cast(ServedRequest, self), comms)
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
        self: ServedRequest[ToolCaller, R], comms: Comms, caller: NoThreadCaller
    ) -> R:
        return self.apply(comms, caller)


@dataclass(frozen=True, kw_only=True)
class ObservedToolRequest[Caller: ToolCaller = ToolCaller, Result: ToolResult = ToolResult](
    ObservedRequest[Result], ServedRequest[Caller, Result], declared_name=None,
):
    """A served request whose run reports progress (ObservedRequest) and stops when cancelled."""


class HumanToolRequest[Result: ToolResult = ToolResult](
    ServedRequest[ToolCaller, Result], declared_name=None,
):
    """A request only the owner's UI may make: Core checks the human caller here, once,
    and the member answers from comms alone."""

    def apply(self, comms: Comms, caller: ToolCaller) -> Result:
        caller.require_human()
        return self.answered(comms)

    @abstractmethod
    def answered(self, comms: Comms) -> Result: ...


class NativeToolRequest[Caller: ToolCaller = ToolCaller, Result: ToolResult = ToolResult](
    ServedRequest[Caller, Result], NativeRequestTools, NativeToolDeclaration, declared_name=None,
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
    PublishingTool[Caller, Result], ServedRequest[Caller, Result], declared_name=None,
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

    def dropped(self, remote: RequestPoster, dropped: ConnectionDropped) -> dict[str, JsonValue]:
        """A read changes nothing, so it is asked once more on a fresh connection."""
        return remote.exchange(cast(ToolRequest, self))

    def require_read_only(self) -> None:
        """The declared read capability controls access, independently of MCP hints."""

    def board(self, comms: Comms, caller: ToolCaller) -> tuple[str, ...]:
        """A read is not a verb: it carries what it read, and the board stays in comms_board."""
        return ()

    @classmethod
    def require_entry_open(cls, root: Path) -> None:
        """Declared reads remain available to the installer's readiness checks."""


class CoreRead[Result: ToolResult = ToolResult](ReadTool, ServedRequest[ToolCaller, Result], declared_name=None):
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
