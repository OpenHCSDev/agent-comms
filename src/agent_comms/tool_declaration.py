"""Shared tool names and presentation; execution capabilities select their own view."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from .agent_events import ToolStart
from .declared_family import DeclaredFamily


class ToolDeclaration(DeclaredFamily, affix="Tool"):
    acp_kind: ClassVar[str] = "other"
    label: ClassVar[str]

    @classmethod
    def _member_declarations(cls):
        # These existing owners share this registry; imports acquire declarations.
        from . import native_tools, tools

        return super()._member_declarations()

    @classmethod
    def for_name(cls, name: str) -> type[ToolDeclaration] | UndeclaredTool:
        cls._member_declarations()
        try:
            return cls.decode(name.lower())
        except ValueError:
            return UndeclaredTool(name)

    @classmethod
    def caption(cls, name: str, detail: str = "") -> str:
        declaration = cls.for_name(name)
        return declaration.label + (" " + detail.strip() if detail else "")

    @classmethod
    def start(cls, call_id: str, name: str, arguments: dict[str, object], *, detail: str = "") -> ToolStart:
        declaration = cls.for_name(name)
        return ToolStart(call_id, name, cls.caption(name, detail), arguments, declaration.acp_kind)

    @classmethod
    def result_diff(cls, result, ok):
        """Opaque metadata cannot invent edit evidence."""
        return None


@dataclass(frozen=True)
class UndeclaredTool(ToolDeclaration):
    """An external name is visible as supplied, without becoming a declaration."""

    declared_name = None
    name: str

    @property
    def label(self) -> str:
        return self.name
