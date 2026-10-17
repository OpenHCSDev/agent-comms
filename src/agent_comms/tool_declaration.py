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

    @classmethod
    def for_name(cls, name: str) -> type[ToolDeclaration] | UndeclaredTool:
        member = cls.member_named(name.lower())
        return UndeclaredTool(name) if member is None else member

    @classmethod
    def caption(cls, name: str, arguments: ToolArguments) -> str:
        declaration = cls.for_name(name)
        preview = declaration.caption_subject(arguments)
        caption = declaration.label + (" " + preview if preview else "")
        return ' '.join(declaration.bounded_caption(caption, cls.CAPTION_CHARS).split())

    @classmethod
    def bounded_caption(cls, caption: str, limit: int) -> str:
        return caption if len(caption) <= limit else caption[:limit - 1] + '…'

    @classmethod
    def caption_subject(cls, arguments: ToolArguments) -> str:
        return ''

    @classmethod
    def recorded_arguments(cls, schema: type[ToolInput], raw: JsonValue) -> ToolArguments:
        """An extension's opaque arguments are not a known coding-tool schema."""
        return ToolArguments(raw)

    @classmethod
    def file_location(cls, arguments: ToolArguments) -> str | None:
        return None

    @classmethod
    def result_diff(cls, result, ok):
        return None


@dataclass(frozen=True, kw_only=True)
class PathTool:
    """Declared file input shared by execution and recorded presentation."""
    path: str
    caption_field: ClassVar[str] = 'path'

    @classmethod
    def bounded_caption(cls, caption: str, limit: int) -> str:
        if len(caption) <= limit:
            return caption
        tail = limit - len(cls.label) - 2
        return cls.label + ' …' + caption[-tail:]

    @classmethod
    def file_location(cls, arguments: DecodedArguments) -> str:
        if arguments.input.path is None:
            raise ValueError('File tool input has no declared path')
        return arguments.input.path


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
        return ToolDeclaration.caption(self.name, self.arguments)

    @projected(view="wire", persist=False)
    def kind(self) -> str:
        return ToolDeclaration.for_name(self.name).acp_kind

    @projected(view="wire", persist=False)
    def detail(self) -> str:
        return ToolDeclaration.for_name(self.name).caption_subject(self.arguments)

    @projected(view="wire", persist=False)
    def path(self) -> str | None:
        return ToolDeclaration.for_name(self.name).file_location(self.arguments)

    @property
    def locations(self) -> tuple[str, ...]:
        path = self.path
        return () if path is None else (path,)


@dataclass(frozen=True)
class ToolArguments:
    raw_input: JsonValue


@dataclass(frozen=True)
class DecodedArguments(ToolArguments):
    input: ToolInput


class ToolInput(WireModel):
    """A protocol's declared fields; unknown input stays in the original value."""
    model_config = ConfigDict(extra='allow')
    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)
    path: str | None = Field(default=None, alias='path', validation_alias=AliasChoices('path'))
    command: str | None = None
    pattern: str | None = None
    caption_codec: ClassVar[TypeAdapter] = TypeAdapter(StrictStr)

    def caption_for(self, field: str) -> str:
        if field not in type(self).model_fields:
            raise KeyError(f'Undeclared caption field: {field}')
        return self.caption_codec.validate_python(getattr(self, field))


class ClaudeToolInput(ToolInput):
    path: str | None = Field(default=None, alias='file_path',
                             validation_alias=AliasChoices('file_path', 'notebook_path', 'path'))
    description: str | None = None
    url: str | None = None
    query: str | None = None
    skill: str | None = None


class OpenCodeToolInput(ToolInput):
    path: str | None = Field(default=None, alias='filePath', validation_alias=AliasChoices('filePath', 'path'))


class RecordedToolInput(ClaudeToolInput, OpenCodeToolInput):
    path: str | None = Field(default=None, validation_alias=AliasChoices(
        *(path[0] for model in (ToolInput, *ToolInput.__subclasses__())
          for path in model.model_fields['path'].validation_alias.convert_to_aliases())))
