"""Tool requests as the wire declares them: fields, names, schemas, results and how a remote
caller posts them. Nothing here serves a request; agent_comms.transport.tool_request composes this
declaration with ServedRequest for the Core that runs it, so a client imports no Core."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from types import get_original_bases
from typing import Annotated, ClassVar, ParamSpec, Self, TypeVar, cast, get_args, get_origin

from pydantic import JsonValue
from typing_extensions import TypeVarTuple

from agent_comms.coordination.errors import CoordinationReadUnavailable
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import (
    ArgumentsExclude,
    ContextSource,
    DefaultNonNull,
    Description,
    FieldCodec,
    FieldDirective,
    NullMember,
    SingleFieldValue,
    WireName,
    WireTransient,
)
from agent_comms.storage.maintenance_barrier import FreshIngress
from agent_comms.transport.progress import WireExportObserver
from agent_comms.transport.tool_declaration import ToolDeclaration
from agent_comms.transport.tool_result import ToolResult


class ArgumentTool:
    """One declared execution record supplies validation and native serialization."""

    family_discriminator: ClassVar[str]
    """The host family's record tag, which the native argument object leaves out."""

    @property
    def arguments(self) -> dict[str, object]:
        # The native argument object has no family envelope. Its contents come
        # entirely from the record codec, never a retained raw dictionary.
        match FieldCodec.project(self, ArgumentsExclude):
            case dict() as encoded:
                return {key: value for key, value in encoded.items() if key != self.family_discriminator}
            case encoded:
                raise TypeError(f"{type(self).__name__} projects to {encoded!r}, not a record")


class ContextBinding(DeclaredFamily, affix="Binding"):
    """A field the UI fills from its selected context; the schema publishes the binding's name."""


class SubjectBinding(ContextBinding):
    """The field is the selected subject thread."""


def tool_field(description: str, *directives: FieldDirective) -> tuple[FieldDirective, ...]:
    """A tool argument's directives: its description, a non-null default, and whatever else the
    field declares (WireName, ContextSource, WireChoices)."""
    return (Description(description), DefaultNonNull(), *directives)


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


class ToolRequest[Result: ToolResult = ToolResult](
    FreshIngress, ArgumentTool, ToolDeclaration, declared_name=None
):
    """A tool request as the wire declares it: its fields, name, schema and result. Serving it
    (decoding a call, running apply under a caller) is ServedRequest's, in agent_comms.transport.tool_request."""

    description: ClassVar[str]
    def is_read_only(self) -> bool:
        return False
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
        return {
            "name": cls.declared_name,
            "label": cls.label,
            "description": cls.description,
            "parameters": FieldCodec.record_schema(cls),
        }

    def posted(self, remote: RequestPoster) -> dict[str, JsonValue]:
        """How a remote access sends this request: one posted call and its reply."""
        return remote.post(self)

    def dropped(self, remote: RequestPoster, dropped: ConnectionDropped) -> dict[str, JsonValue]:
        """A call that may change the bus is never sent twice: Core may already have run it, so
        the caller hears the connection failed."""
        raise dropped

    def unavailable(self, error: OSError) -> dict[str, JsonValue]:
        """A failed transport does not establish whether a write ran."""
        raise error

    @classmethod
    def result_type(cls) -> type[Result]:
        """The result this request declares in its class header, followed through every
        intermediate generic parent; a remote reply decodes into it. Every path's binding is
        collected and the most derived wins, so a result declared on one parent refines the
        default another parent inherited. Unrelated bindings conflict."""
        return cast(type[Result], declared_result(cls, ToolRequest.__type_params__[0]))


def declared_result(cls: type, declared: TypeVar | ParamSpec | TypeVarTuple) -> object:
    """The argument bound for `declared` in the class headers of `cls`, followed through
    every intermediate generic parent; the most derived of unrelated bindings wins."""
    bindings: dict[object, list[object]] = {}
    for declaration in cls.__mro__:
        for base in get_original_bases(declaration):
            match get_origin(base):
                case type() as origin:
                    for parameter, argument in zip(
                        origin.__type_params__, get_args(base), strict=False
                    ):
                        bindings.setdefault(parameter, []).append(argument)

    def declared_by(parameter: object, seen: frozenset[object]) -> list[object]:
        found: list[object] = []
        for argument in bindings.get(parameter, ()):
            if isinstance(argument, TypeVar):
                if argument not in seen:
                    found.extend(declared_by(argument, seen | {argument}))
            else:
                found.append(argument)
        return found

    default = declared.__default__
    candidates = list(dict.fromkeys(
        found for found in declared_by(declared, frozenset({declared})) if found is not default
    )) or [default]
    if len(candidates) == 1:
        return candidates[0]
    for result in candidates:
        if isinstance(result, type) and all(
                isinstance(other, type) and issubclass(result, other) for other in candidates):
            return result
    raise TypeError(f"{cls.__name__} declares unrelated results: {candidates}")


@dataclass(frozen=True, kw_only=True)
class ObservedRequest[Result: ToolResult = ToolResult](ToolRequest[Result], declared_name=None):
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


class ReadRequest[Result: ToolResult = ToolResult](ToolRequest[Result], declared_name=None):
    """A request that changes nothing on the bus, as the wire declares it."""

    bus_effect: ClassVar[str] = "Reads only; changes nothing on the bus."
    def is_read_only(self) -> bool:
        return True

    def dropped(self, remote: RequestPoster, dropped: ConnectionDropped) -> dict[str, JsonValue]:
        """A read changes nothing, so it is asked once more on a fresh connection."""
        return remote.exchange(self)

    def unavailable(self, error: OSError) -> dict[str, JsonValue]:
        """The source answered no read; retain its last value until it recovers."""
        raise RemoteReadUnavailable(str(error)) from error


class RemoteReadUnavailable(CoordinationReadUnavailable, ConnectionError):
    """A remote read acquired no answer; its transport failure remains its cause."""

    def __new__(cls, *args: object) -> Self:
        # The ConnectionError role owns OSError's native allocation layout.
        # RuntimeError.__new__ rejects it when Python's unpickler calls it.
        return ConnectionError.__new__(cls, *args)


@dataclass(frozen=True, kw_only=True)
class ThreadReadRequest[Result: ToolResult = ToolResult](ReadRequest[Result], declared_name=None):
    """A read about one thread; empty names the caller's own."""

    thread: Annotated[str, *tool_field("Thread to read; omit to read your own")] = ""


class ConnectionDropped(ConnectionError):
    """The connection to Core dropped during a call: whether Core ran it is unknown."""


class RequestPoster(ABC):
    """Who sends a request to a Core across the connector; each request picks which way it is
    sent, plainly or with its progress and cancel streamed."""

    @abstractmethod
    def post[R: ToolResult](self, request: ToolRequest[R]) -> dict[str, JsonValue]: ...

    @abstractmethod
    def exchange[R: ToolResult](
        self, request: ToolRequest[R],
    ) -> dict[str, JsonValue]:
        """One round trip on the kept connection; a drop raises ConnectionDropped."""

    @abstractmethod
    def post_observed[R: ToolResult](
        self, request: ObservedRequest[R],
    ) -> dict[str, JsonValue]: ...


class CoreAccess(ABC):
    """Where a UI's Core requests are answered: in this process or across the connector. The
    UI holds the declaration and gets its declared result, whichever side answers."""

    @abstractmethod
    def call[R: ToolResult](self, request: ToolRequest[R]) -> R:
        """The request's declared result, from whichever Core this access reaches."""
