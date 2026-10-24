"""Shared tool names and presentation; execution capabilities select their own view."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, ClassVar, Self

from .declared_family import DeclaredFamily, UnknownFamilyMember
from .field_codec import FieldCodec, MalformedRecord, NullMember, WireName
from .native_tool_call import ToolCallId
from .recorded_time import EventTime
from .tool_denial import SelectedToolDenied
from .tool_results import NoToolDiff


def one_line(text: str, limit: int) -> str:
    return " ".join(text.split())[:limit]


@dataclass(frozen=True)
class ReplacementEdit:
    old_text: Annotated[str, WireName("oldText")]
    new_text: Annotated[str, WireName("newText")]


@dataclass(frozen=True)
class EditCall:
    id: str
    at: EventTime
    thread: str
    path: str
    hunks: tuple[ReplacementEdit, ...]
    problem: str


class ToolAction:
    """A tool capability, composed beside its declaration family."""

    members: ClassVar[set[type[ToolAction]]] = set()

    def __init_subclass__(cls) -> None:
        super().__init_subclass__()
        if ToolAction in cls.__bases__:
            ToolAction.members.add(cls)


class SearchAction(ToolAction):
    """Find matching information."""


class ReadAction(ToolAction):
    """Read existing content."""


class ExecuteAction(ToolAction):
    """Execute a command."""


class EditAction(ToolAction):
    """Change content."""


class OtherAction(ToolAction):
    """A tool with no more specific action."""


class ToolDeclaration(DeclaredFamily, affix="Tool"):
    family_discriminator: ClassVar[str] = "tool"
    label: ClassVar[str]

    @classmethod
    def declared_action(cls) -> type[ToolAction]:
        """The first action capability in the tool's C3 MRO."""
        return next(
            (action for base in cls.__mro__ for action in ToolAction.members if base is action),
            OtherAction,
        )

    @classmethod
    def _member_declarations(cls):
        # These existing owners share this registry; imports acquire declarations.
        from . import native_tools, tools

        return super()._member_declarations()

    @classmethod
    def for_name(cls, name: str) -> type[ToolDeclaration] | UndeclaredTool:
        try:
            return cls.member_named(name.lower())
        except UnknownFamilyMember:
            return UndeclaredTool(name)

    @classmethod
    def parse_arguments(cls, arguments: Mapping[str, object], *, call_id: ToolCallId) -> Self:
        if cls.family_discriminator in arguments:
            raise SelectedToolDenied("Native arguments cannot contain a family selector")
        selected = {cls.family_discriminator: cls.declared_name}
        return FieldCodec.decode_bound(cls, {**selected, **arguments}, call_id=call_id)

    @classmethod
    def record(
        cls, name: str, arguments: object, *, call_id: str
    ) -> ToolDeclaration | NoToolRecord:
        """The call decoded once as Core's tool record, or no record when Core does not decode it."""
        try:
            decoded = FieldCodec.decode(dict[str, object], arguments)
            return cls.for_name(name).parse_arguments(
                decoded, call_id=FieldCodec.decode(ToolCallId, call_id))
        except MalformedRecord, SelectedToolDenied:
            return NoToolRecord()

    def recorded_call(self, shown):
        """The shown call, carrying this record."""
        return shown.recorded(self)

    def edit_calls(
        self, call_id: str, at: EventTime, thread: str, problem: str
    ) -> tuple[EditCall, ...]:
        return ()

    def reads(self) -> str:
        """What the call names after its label: nothing unless the tool says."""
        return ""

    def digest(self) -> str:
        """What the call does in one line, enough to judge it."""
        return f"{self.declared_name} {one_line(self.reads(), 260)}"

    def call_digest(self, name: str, arguments: object) -> str:
        """A call in one line: its record's digest."""
        return self.digest()

    @classmethod
    def caption(cls, name: str, tool: ToolDeclaration | NoToolRecord) -> str:
        """A call reads as its label, then what its record names; with no record, its label."""
        reads = one_line(text := tool.reads(), len(text))
        return cls.for_name(name).label + (" " + reads if reads else "")

    @classmethod
    def result_diff(cls, result, ok):
        """Opaque metadata cannot invent edit evidence."""
        return NoToolDiff()


@dataclass(frozen=True)
class NoToolRecord(NullMember):
    """Core did not decode the call into a tool record; its raw arguments stand."""

    def reads(self) -> str:
        return ""

    def edit_calls(
        self, call_id: str, at: EventTime, thread: str, problem: str
    ) -> tuple[EditCall, ...]:
        return ()

    def call_digest(self, name: str, arguments: object) -> str:
        return f"{name} {one_line(json.dumps(arguments), 260)}"

    def recorded_call(self, shown):
        return shown


@dataclass(frozen=True)
class UndeclaredTool(ToolDeclaration, declared_name=None):
    """An external name is visible as supplied, without becoming a declaration."""

    name: str

    @property
    def label(self) -> str:
        return self.name
