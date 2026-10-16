"""Shared tool names and presentation; execution capabilities select their own view."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, ClassVar

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
    def caption(cls, name: str, *, detail: str = "", path: str | None = None) -> str:
        declaration = cls.for_name(name)
        preview = path if path is not None else detail.strip()
        caption = declaration.label + (" " + preview if preview else "")
        if len(caption) > cls.CAPTION_CHARS:
            caption = caption[:cls.CAPTION_CHARS - 1] + '…'
        return ' '.join(caption.split())

    @classmethod
    def file_location(cls, decode: Callable[[], str]) -> str | None:
        """Opaque extensions never interpret a known tool's argument schema."""
        return None

    @classmethod
    def command_detail(cls, decode: Callable[[], str]) -> str:
        return ""

    @classmethod
    def start(cls, call_id: str, name: str, arguments: dict[str, object], *,
              detail: str = "", path: str | None = None) -> ToolStart:
        declaration = cls.for_name(name)
        return ToolStart(call_id, name, cls.caption(name, detail=detail, path=path),
                         arguments, declaration.acp_kind, path=path, detail=detail)

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
    def file_location(cls, decode: Callable[[], str]) -> str:
        return decode()


@dataclass(frozen=True)
class UndeclaredTool(ToolDeclaration):
    """An external name is visible as supplied, without becoming a declaration."""

    declared_name = None
    name: str

    @property
    def label(self) -> str:
        return self.name
