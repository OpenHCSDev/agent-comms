"""Tool-call decoding shared by every runtime that reports calls in its own spelling.

A runtime's tool call arrives as the runtime sent it: a call id, the tool's name and its arguments.
When the name has a declared argument shape and the values fit it, the call is decoded once into
Core's tool record, and an edit carries the patch it reports. A call Core cannot decode is shown as
sent, under the runtime's own name.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import ClassVar

from pydantic import AliasChoices, Field, JsonValue, ValidationError

from agent_comms.wire_models import DeclaredWireFamily, WireModel
from .declared_family import UniqueExternalSelector
from .field_codec import FieldCodec, MalformedRecord
from .native_tools import BashTool, EditTool, GlobTool, GrepTool, ReadTool, WriteTool
from .tool_declaration import ReplacementEdit, ToolDeclaration
from .tool_results import ToolDiff
from .transcript_events import ToolEndTranscript, ToolStartTranscript


@dataclass(frozen=True)
class ToolCall:
    """A call as the runtime sent it, with the same preview and history as a native tool."""

    id: str
    name: str
    arguments: dict[str, JsonValue]

    @property
    def title(self) -> str:
        return ToolDeclaration.for_name(self.name).label

    def started(self, position: int | str, timestamp: float) -> ToolStartTranscript:
        return ToolStartTranscript(
            position=position, timestamp=timestamp, tool_call_id=self.id, tool_name=self.name, raw_input=self.arguments
        )

    def ended(self, text: str, ok: bool, position: int | str, timestamp: float) -> ToolEndTranscript:
        return ToolEndTranscript(
            position=position, timestamp=timestamp, tool_call_id=self.id, tool_name=self.name, text=text, ok=ok
        )

    def finished(
        self, text: str, ok: bool, position: int | str, timestamp: float,
    ) -> tuple[ToolStartTranscript, ToolEndTranscript]:
        return self.started(position, timestamp), self.ended(text, ok, position, timestamp)

    def recorded(self, tool: ToolDeclaration) -> DecodedToolCall:
        return DecodedToolCall(self.id, self.name, self.arguments, tool)


@dataclass(frozen=True)
class DecodedToolCall(ToolCall):
    """A call decoded once into Core's tool record."""

    tool: ToolDeclaration

    @property
    def title(self) -> str:
        return ToolDeclaration.caption(self.name, self.tool)

    def started(self, position: int | str, timestamp: float) -> ToolStartTranscript:
        return replace(super().started(position, timestamp), tool=self.tool)

    def patched(self, diff: ToolDiff) -> PatchedToolCall:
        return PatchedToolCall(self.id, self.name, self.arguments, self.tool, diff)


@dataclass(frozen=True)
class PatchedToolCall(DecodedToolCall):
    """An edit that reports the patch it made."""

    diff: ToolDiff

    def ended(self, text: str, ok: bool, position: int | str, timestamp: float) -> ToolEndTranscript:
        return replace(super().ended(text, ok, position, timestamp), diff=self.diff)


class CliToolArguments(UniqueExternalSelector, DeclaredWireFamily, WireModel, affix="Arguments"):
    """A runtime's arguments for one Core tool in its own spelling (Claude Code's, OpenCode's), read
    once into the Core record; the runtime's other keys stay in its raw input."""

    cli_names: ClassVar[tuple[str, ...]] = ()
    model_config = {"extra": "allow"}

    @classmethod
    def selection_key(cls) -> tuple[str, ...]:
        return cls.cli_names

    @classmethod
    def decode(cls, sent: ToolCall, arguments: Mapping[str, JsonValue]) -> ToolCall:
        """The call decoded when its name has a declared shape that its values fit, else as sent.

        The model can send values that do not fit (Claude refuses them with an error result)."""
        shapes = [shape for shape in cls.members_with(cls) if sent.name in shape.cli_names]
        if not shapes:
            return sent
        if len(shapes) > 1:
            owners = ", ".join(shape.__name__ for shape in shapes)
            raise ValueError(f"Ambiguous CLI tool name {sent.name!r}: {owners}")
        shape, = shapes
        try:
            return shape.model_validate(arguments).call(sent.id)
        except ValidationError, MalformedRecord:  # kept as sent; its runtime answers the refusal
            return sent

    @abstractmethod
    def record(self, *, call_id: str) -> ToolDeclaration: ...

    def call(self, call_id: str) -> DecodedToolCall:
        tool = self.record(call_id=call_id)
        name = FieldCodec.encode(type(tool), type[ToolDeclaration])
        shown = ToolCall(call_id, name, self.model_dump(exclude_unset=True))
        return shown.recorded(tool)


class CliCommandArguments(CliToolArguments):
    cli_names = ("Bash", "bash")
    command: str

    def record(self, *, call_id: str) -> BashTool:
        return BashTool(call_id=call_id, command=self.command)


class CliPatternArguments(CliToolArguments):
    """A search; its scope is optional, so the search record's own codec reads it."""

    cli_names: ClassVar[tuple[str, ...]] = ("Grep", "grep")
    search: ClassVar[type[GrepTool | GlobTool]] = GrepTool
    pattern: str

    def record(self, *, call_id: str) -> GrepTool | GlobTool:
        scope = self.model_dump(include={"pattern", "path"})
        return self.search.parse_arguments(scope, call_id=call_id)


class CliGlobArguments(CliPatternArguments):
    cli_names = ("Glob", "glob")
    search = GlobTool


class CliPathArguments(CliToolArguments):
    path: str = Field(validation_alias=AliasChoices("file_path", "filePath"))


class CliReadArguments(CliPathArguments):
    cli_names = ("Read", "read")

    def record(self, *, call_id: str) -> ReadTool:
        return ReadTool(call_id=call_id, path=self.path)


class CliReplacement(WireModel):
    old_string: str = Field(validation_alias=AliasChoices("old_string", "oldString"))
    new_string: str = Field(validation_alias=AliasChoices("new_string", "newString"))


class CliReplacingArguments(CliPathArguments):
    """A file edit by replacements, however many the runtime's call shape carries."""

    @abstractmethod
    def replacements(self) -> Sequence[CliReplacement]: ...

    def record(self, *, call_id: str) -> EditTool:
        edits = tuple(
            ReplacementEdit(old_text=edit.old_string, new_text=edit.new_string)
            for edit in self.replacements()
        )
        return EditTool(call_id=call_id, path=self.path, edits=edits)

    def call(self, call_id: str) -> PatchedToolCall:
        patches = (
            ToolDiff.from_versions(self.path, edit.old_string, edit.new_string)
            for edit in self.replacements()
        )
        return (
            super()
            .call(call_id)
            .patched(ToolDiff("".join(part.text for patch in patches for part in patch.parts())))
        )


class CliEditArguments(CliReplacement, CliReplacingArguments):
    """One replacement, its fields in the call itself (Claude Code's Edit, OpenCode's edit)."""

    cli_names = ("Edit", "edit")

    def replacements(self) -> Sequence[CliReplacement]:
        return (self,)


class CliMultiEditArguments(CliReplacingArguments):
    """A list of replacements (Claude Code's MultiEdit)."""

    cli_names = ("MultiEdit",)
    edits: list[CliReplacement]

    def replacements(self) -> Sequence[CliReplacement]:
        return self.edits


class CliWriteArguments(CliPathArguments):
    cli_names = ("Write", "write")
    content: str

    def record(self, *, call_id: str) -> WriteTool:
        return WriteTool(call_id=call_id, path=self.path, content=self.content)

    def call(self, call_id: str) -> DecodedToolCall:
        call = super().call(call_id)
        before = self.path + " (previous content not supplied)"
        diff = ToolDiff.between(before, self.path, "", self.content)
        return diff.patch(call)
