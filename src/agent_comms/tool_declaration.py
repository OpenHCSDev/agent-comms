"""Shared tool names and presentation; execution capabilities select their own view."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from typing import ClassVar, Self

from .declared_family import DeclaredFamily
from .field_codec import FieldCodec, MalformedRecord
from .native_tool_call import SelectedToolDenied


def one_line(text: str, limit: int) -> str:
    return ' '.join(text.split())[:limit]


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
            return cls.decode(name.lower())
        except ValueError:
            return UndeclaredTool(name)

    @classmethod
    def parse_arguments(cls, arguments: Mapping[str, object]) -> Self:
        if cls.family_discriminator in arguments:
            raise SelectedToolDenied("Native arguments cannot contain a family selector")
        selected = {cls.family_discriminator: cls.declared_name}
        try:
            return FieldCodec.decode(cls, {**selected, **arguments})
        except MalformedRecord as error:
            raise SelectedToolDenied(f"Invalid {cls.declared_name} arguments: {error}") from error

    @classmethod
    def record(cls, name: str, arguments: object) -> ToolDeclaration | None:
        """The call decoded once as Core's tool record, or None when Core does not decode it."""
        try:
            decoded = FieldCodec.decode(dict[str, object], arguments)
            return cls.for_name(name).parse_arguments(decoded)
        except (MalformedRecord, SelectedToolDenied):
            return None

    def reads(self) -> str:
        """What the call names after its label: nothing unless the tool says."""
        return ''

    @classmethod
    def caption(cls, name: str, tool: ToolDeclaration | None) -> str:
        """A call reads as its label, then what its record names; with no record, its label."""
        reads = one_line(text := tool.reads(), len(text)) if tool else ''
        return cls.for_name(name).label + (' ' + reads if reads else '')

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
