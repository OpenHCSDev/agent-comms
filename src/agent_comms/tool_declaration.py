"""Shared tool names and presentation; execution capabilities select their own view."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import ClassVar, Self

from .declared_family import DeclaredFamily, UnknownFamilyMember
from .field_codec import FieldCodec, MalformedRecord
from .native_tool_call import SelectedToolDenied
from .recorded_time import EventTime


def one_line(text: str, limit: int) -> str:
    return ' '.join(text.split())[:limit]


@dataclass(frozen=True)
class ReplacementEdit:
    old_text: str = field(metadata={"wire_name": "oldText"})
    new_text: str = field(metadata={"wire_name": "newText"})


@dataclass(frozen=True)
class EditCall:
    id: str
    at: EventTime
    thread: str
    path: str
    hunks: tuple[ReplacementEdit, ...]
    problem: str


class ToolDeclaration(DeclaredFamily, affix="Tool"):
    family_discriminator: ClassVar[str] = "tool"
    acp_kind: ClassVar[str] = "other"
    label: ClassVar[str]

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
    def parse_arguments(cls, arguments: Mapping[str, object], *, call_id: str) -> Self:
        if cls.family_discriminator in arguments:
            raise SelectedToolDenied("Native arguments cannot contain a family selector")
        selected = {cls.family_discriminator: cls.declared_name}
        return FieldCodec.decode_bound(cls, {**selected, **arguments}, call_id=call_id)

    @classmethod
    def record(cls, name: str, arguments: object, *, call_id: str) -> ToolDeclaration | None:
        """The call decoded once as Core's tool record, or None when Core does not decode it."""
        try:
            decoded = FieldCodec.decode(dict[str, object], arguments)
            return cls.for_name(name).parse_arguments(decoded, call_id=call_id)
        except (MalformedRecord, SelectedToolDenied):
            return None

    def edit_calls(self, call_id: str, at: EventTime, thread: str, problem: str) -> tuple[EditCall, ...]:
        return ()

    def reads(self) -> str:
        """What the call names after its label: nothing unless the tool says."""
        return ''

    def digest(self) -> str:
        """What the call does in one line, enough to judge it."""
        return f'{self.declared_name} {one_line(self.reads(), 260)}'

    @classmethod
    def caption(cls, name: str, tool: ToolDeclaration | None) -> str:
        """A call reads as its label, then what its record names; with no record, its label."""
        reads = one_line(text := tool.reads(), len(text)) if tool else ''
        return cls.for_name(name).label + (' ' + reads if reads else '')

    @classmethod
    def call_digest(cls, name: str, tool: ToolDeclaration | None, arguments: object) -> str:
        """A call in one line: its record's digest; with no record, its name and raw arguments."""
        return tool.digest() if tool else f'{name} {one_line(json.dumps(arguments), 260)}'

    @classmethod
    def result_diff(cls, result, ok):
        """Opaque metadata cannot invent edit evidence."""
        return None


@dataclass(frozen=True)
class UndeclaredTool(ToolDeclaration, declared_name=None):
    """An external name is visible as supplied, without becoming a declaration."""

    name: str

    @property
    def label(self) -> str:
        return self.name
