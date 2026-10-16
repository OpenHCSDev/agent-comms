"""Shared tool names and presentation; execution capabilities select their own view."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from .agent_events import ToolStart
from .declared_family import DeclaredFamily


class ToolDeclaration(DeclaredFamily, affix="Tool"):
    acp_kind: ClassVar[str] = "other"
    label: ClassVar[str] = ""

    @classmethod
    def _member_declarations(cls):
        # These existing declaration owners share this registry. Imports acquire
        # their classes, not another name list or a second dispatch table.
        from . import native_tools, tools

        return super()._member_declarations()

    @classmethod
    def for_name(cls, name: str) -> type[ToolDeclaration]:
        cls._member_declarations()
        try:
            return cls.decode(name.lower())
        except ValueError:
            return cls

    @classmethod
    def start(cls, call_id: str, name: str, arguments: dict[str, Any]) -> ToolStart:
        declaration = cls.for_name(name)
        detail = declaration.detail(arguments)
        action = declaration.label or name.replace("_", " ").title()
        if detail:
            try:
                text = detail if isinstance(detail, str) else json.dumps(detail)
            except (TypeError, ValueError):
                text = str(detail)
            action += " " + text.strip()
        return ToolStart(call_id, name, action, arguments, declaration.acp_kind)

    @classmethod
    def detail(cls, arguments: dict[str, Any]) -> object:
        return None

    @classmethod
    def result_diff(cls, result, ok):
        """Unknown tool metadata stays opaque; it cannot invent edit evidence."""
        return None
