"""Shared tool names and presentation; execution capabilities select their own view."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from .agent_events import ToolStart
from .declared_family import DeclaredFamily


class ToolDeclaration(DeclaredFamily, affix="Tool"):
    CAPTION_CHARS: ClassVar[int] = 80
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
    def caption(cls, name: str, arguments: object, *, detail: str = "") -> str:
        declaration = cls.for_name(name)
        preview = detail.strip() or declaration.input_preview(arguments)
        caption = declaration.label + (" " + preview if preview else "")
        if len(caption) > cls.CAPTION_CHARS:
            caption = caption[:cls.CAPTION_CHARS - 1] + '…'
        return ' '.join(caption.split())

    @classmethod
    def input_preview(cls, arguments: object) -> str:
        """Unknown metadata has no declared preview; never dump raw input."""
        return ""

    @staticmethod
    def argument_text(arguments: object, name: str) -> str:
        if not isinstance(arguments, dict):
            return ""
        value = arguments.get(name)
        return value if isinstance(value, str) else ""

    @classmethod
    def start(cls, call_id: str, name: str, arguments: dict[str, object], *, detail: str = "") -> ToolStart:
        declaration = cls.for_name(name)
        return ToolStart(call_id, name, cls.caption(name, arguments, detail=detail), arguments, declaration.acp_kind)

    @classmethod
    def result_diff(cls, result, ok):
        """Opaque metadata cannot invent edit evidence."""
        return None


@dataclass(frozen=True, kw_only=True)
class PathTool:
    """Declared file input shared by execution and recorded presentation."""
    path: str

    @property
    def detail(self) -> str:
        return self.path

    @classmethod
    def recorded_path(cls, arguments: object) -> str:
        # These are the current Pi, Claude and OpenCode input spellings.
        return next((value for name in ('path', 'file_path', 'filePath')
                     if (value := ToolDeclaration.argument_text(arguments, name))), '')

    @classmethod
    def input_preview(cls, arguments: object) -> str:
        return cls.recorded_path(arguments)


@dataclass(frozen=True)
class UndeclaredTool(ToolDeclaration):
    """An external name is visible as supplied, without becoming a declaration."""

    declared_name = None
    name: str

    @property
    def label(self) -> str:
        return self.name
