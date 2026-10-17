"""Tool declarations and boundary-decoded arguments own recorded presentation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from pydantic import AliasChoices, ConfigDict, Field, JsonValue, StrictStr, TypeAdapter

from .cli_protocol import WireModel
from .declared_family import DeclaredFamily
from .field_codec import projected


class ToolDeclaration(DeclaredFamily, affix="Tool", discovery_package="agent_comms.tools"):
    CAPTION_CHARS: ClassVar[int] = 80
    acp_kind: ClassVar[str] = "other"
    label: ClassVar[str]
    caption_field: ClassVar[str | None] = None

    @classmethod
    def for_name(cls, name: str) -> type[ToolDeclaration] | UndeclaredTool:
        member = cls.member_named(name.lower())
        return UndeclaredTool(name) if member is None else member

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
    def recorded_arguments(cls, schema: type[ToolInput], raw: JsonValue) -> ToolArguments:
        """An extension's opaque arguments are not a known coding-tool schema."""
        return ToolArguments(raw)

    @classmethod
    def file_location(cls, path: str | None) -> str | None:
        return None

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


class ToolCallMetadata:
    """Invocation views consume decoded arguments, never a second set of fields."""
    name: str
    arguments: ToolArguments

    @projected(view="wire", persist=False)
    def title(self) -> str:
        arguments = self.arguments
        return ToolDeclaration.caption(self.name, detail=arguments.caption, path=arguments.path)

    @projected(view="wire", persist=False)
    def kind(self) -> str:
        return ToolDeclaration.for_name(self.name).acp_kind

    @projected(view="wire", persist=False)
    def detail(self) -> str:
        return self.arguments.caption

    @projected(view="wire", persist=False)
    def path(self) -> str | None:
        return self.arguments.path

    @property
    def locations(self) -> tuple[str, ...]:
        path = self.path
        return () if path is None else (path,)


@dataclass(frozen=True)
class ToolArguments:
    raw_input: JsonValue
    path: str | None = None
    caption: str = ""


class ToolInput(WireModel):
    """A protocol's declared fields; unknown input stays in the original value."""
    model_config = ConfigDict(extra='allow')
    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)
    path: str | None = Field(default=None, alias='path', validation_alias=AliasChoices('path'))
    command: str | None = None
    pattern: str | None = None
    caption_codec: ClassVar[TypeAdapter] = TypeAdapter(StrictStr)

    def caption_for(self, field: str | None) -> str:
        value = self.model_dump().get(field)
        return '' if value is None else self.caption_codec.validate_python(value)


class ClaudeToolInput(ToolInput):
    path: str | None = Field(default=None, alias='file_path',
                             validation_alias=AliasChoices('file_path', 'notebook_path', 'path'))
    description: str | None = None
    url: str | None = None
    query: str | None = None
    skill: str | None = None


class OpenCodeToolInput(ToolInput):
    path: str | None = Field(default=None, alias='filePath', validation_alias=AliasChoices('filePath', 'path'))


class RecordedToolInput(ToolInput):
    path: str | None = Field(default=None, validation_alias=AliasChoices(
        *(path[0] for model in (ToolInput, *ToolInput.__subclasses__())
          for path in model.model_fields['path'].validation_alias.convert_to_aliases())))
