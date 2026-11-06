"""Tool requests as the wire declares them: fields, names, schemas, results and how a remote
caller posts them. Nothing here serves a request; agent_comms.tool_request composes this
declaration with ServedRequest for the Core that runs it, so a client imports no Core."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from types import get_original_bases
from typing import Self, Annotated, ClassVar, TypeVar, cast, get_args, get_origin

from pydantic import JsonValue

from .progress import WireExportObserver
from ..nominal.declared_family import DeclaredFamily
from ..nominal.field_codec import (
    ArgumentsExclude,
    ContextSource,
    DefaultNonNull,
    Description,
    FieldCodec,
    FieldDirective,
    NullMember,
    SingleFieldValue,
    WireChoices,
    WireName,
    WireTransient,
)
from ..storage.maintenance_barrier import FreshIngress
from ..tool_declaration import ToolDeclaration
from ..tool_result import ToolResult


class ArgumentTool:
    """One declared execution record supplies validation and native serialization."""

    @property
    def arguments(self) -> dict[str, object]:
        # The native argument object has no family envelope. Its contents come
        # entirely from the record codec, never a retained raw dictionary.
        encoded = FieldCodec.project(self, ArgumentsExclude)
        del encoded[self.family_discriminator]
        return encoded


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


class ToolRequest[Result: ToolResult = ToolResult](FreshIngress, ArgumentTool, ToolDeclaration):
    """A tool request as the wire declares it: its fields, name, schema and result. Serving it
    (decoding a call, running apply under a caller) is ServedRequest's, in agent_comms.tool_request."""

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

    def posted(self, remote: RequestPoster) -> dict[str, JsonValue]:
        """How a remote access sends this request: one posted call and its reply."""
        return remote.post(self)

    def dropped(self, remote: RequestPoster, dropped: ConnectionDropped) -> dict[str, JsonValue]:
        """A call that may change the bus is never sent twice: Core may already have run it, so
        the caller hears the connection failed."""
        raise dropped

    @classmethod
    def result_type(cls) -> type[Result]:
        """The result this request declares in its class header, followed through every
        intermediate generic parent; a remote reply decodes into it. Every path's binding is
        collected and the most derived wins, so a result declared on one parent refines the
        default another parent inherited. Unrelated bindings conflict."""
        return cast(type[Result], declared_result(cls, ToolRequest.__type_params__[0]))


def declared_result(cls: type, declared: TypeVar) -> type:
    """The argument bound for `declared` in the class headers of `cls`, followed through
    every intermediate generic parent; the most derived of unrelated bindings wins."""
    bindings: dict[object, list[object]] = {}
    for declaration in cls.__mro__:
        for base in get_original_bases(declaration):
            origin = get_origin(base)
            for parameter, argument in zip(getattr(origin, "__type_params__", ()), get_args(base)):
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
        self, request: ObservedToolRequest[R],
    ) -> dict[str, JsonValue]: ...


class CoreAccess(ABC):
    """Where a UI's Core requests are answered: in this process or across the connector. The
    UI holds the declaration and gets its declared result, whichever side answers."""

    @abstractmethod
    def call[R: ToolResult](self, request: ToolRequest[R]) -> R:
        """The request's declared result, from whichever Core this access reaches."""
