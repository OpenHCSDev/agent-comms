"""Tool declarations and boundary-decoded arguments own recorded presentation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, TypeVar

from pydantic import JsonValue

from .cli_protocol import WireModel
from .declared_family import DeclaredFamily
from .recorded_time import EventTime


class ToolDeclaration(DeclaredFamily, affix="Tool", discovery_package="agent_comms.tools"):
    CAPTION_CHARS: ClassVar[int] = 80
    acp_kind: ClassVar[str] = "other"
    label: ClassVar[str]

    @classmethod
    def for_name(cls, name: str) -> type[ToolDeclaration] | UndeclaredTool:
        try:
            return cls.decode(name.lower())
        except ValueError:
            return UndeclaredTool(name)

    @classmethod
    def caption(cls, name: str, *, detail: str = "", path: str | None = None) -> str:
        declaration = cls.for_name(name)
        preview = path if path is not None else detail.strip()
        caption = declaration.label + (" " + preview if preview else "")
        return ' '.join(declaration.bounded_caption(caption, cls.CAPTION_CHARS).split())

    @classmethod
    def bounded_caption(cls, caption: str, limit: int) -> str:
        return caption if len(caption) <= limit else caption[:limit - 1] + '…'

    @classmethod
    def recorded_arguments(cls, schema: type[ToolArguments], raw: JsonValue) -> ToolArguments:
        """An extension's opaque arguments are not a known coding-tool schema."""
        return schema()

    @classmethod
    def file_location(cls, path: str | None) -> str | None:
        return None

    @classmethod
    def command_detail(cls, command: str) -> str:
        return ""

    @classmethod
    def result_diff(cls, result, ok):
        return None


@dataclass(frozen=True, kw_only=True)
class PathTool:
    """Declared file input shared by execution and recorded presentation."""
    path: str

    @property
    def detail(self) -> str:
        return self.path

    @classmethod
    def bounded_caption(cls, caption: str, limit: int) -> str:
        if len(caption) <= limit:
            return caption
        tail = limit - len(cls.label) - 2
        return cls.label + ' …' + caption[-tail:]

    @classmethod
    def file_location(cls, path: str | None) -> str:
        if path is None:
            raise ValueError('File tool input has no declared path')
        return path


@dataclass(frozen=True)
class UndeclaredTool(ToolDeclaration):
    declared_name = None
    name: str

    @property
    def label(self) -> str:
        return self.name


class ToolLocations:
    """Tool metadata exposes its zero or one file location."""
    path: str | None

    @property
    def locations(self) -> tuple[str, ...]:
        return () if self.path is None else (self.path,)


@dataclass(frozen=True)
class ToolCall:
    """A decoded invocation; CLI drafts compose their lifetime onto this value."""
    started_at: EventTime
    id: str
    name: str
    arguments: JsonValue
    detail: str = ""
    path: str | None = None

    @property
    def title(self) -> str:
        return ToolDeclaration.caption(self.name, detail=self.detail, path=self.path)

    @property
    def kind(self) -> str:
        return ToolDeclaration.for_name(self.name).acp_kind


Call = TypeVar('Call', bound=ToolCall)


class ToolArguments(WireModel):
    """One decoding/build algorithm; foreign boundaries declare only aliases."""
    path: str | None = None
    command: str = ""

    @classmethod
    def build_call(cls, started_at: EventTime, call_id: str, name: str, raw: JsonValue, *,
                   call_type: type[Call] = ToolCall) -> Call:
        declaration = ToolDeclaration.for_name(name)
        arguments = declaration.recorded_arguments(cls, raw)
        return call_type(started_at, call_id, name, raw,
                         detail=declaration.command_detail(arguments.command),
                         path=declaration.file_location(arguments.path))
