"""Typed Pi argument records, native execution capabilities and cooperative claims."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from .envelope_claim_transitions import ExistingFileClaim, FileClaimPath, WritableFileClaim
from .field_codec import FieldCodec
from .storage_rules import WriteStorageEffect, physical_path, shell_storage_effects
from .tool_declaration import ToolDeclaration, one_line


class NativeExecutableTool(ToolDeclaration):
    """A declared tool participating in the existing native owner check."""

    claim: ClassVar[None] = None

    @classmethod
    def storage_effects(cls, cwd):
        return ()

    @classmethod
    @abstractmethod
    def create_call(cls, call_id: str, arguments: dict[str, object]): ...

    @classmethod
    @abstractmethod
    def native_extension(cls, package: Path) -> Path: ...

    @classmethod
    def from_call(cls, name: str, arguments: object, *, call_id: str):
        decoded = FieldCodec.decode(dict[str, object], arguments)
        return cls.field_value(name).create_call(call_id, decoded)


class NativeRequestTools:
    """Core's existing CLI decodes and executes its declared request fields."""

    @classmethod
    def catalog(cls) -> list[dict[str, object]]:
        return [tool.schema() for tool in ToolDeclaration.members_with(cls)]

    @classmethod
    def create_call(cls, call_id: str, arguments: dict[str, object]):
        from .channel_coding_tools import CoordinationCall

        return CoordinationCall(call_id, cls, arguments)

    @classmethod
    def native_extension(cls, package: Path) -> Path:
        return package / "agent-comms-extensions/global-agent-comms/index.mjs"


class ArgumentTool:
    """One declared execution record supplies validation and native serialization."""

    @property
    def arguments(self) -> dict[str, object]:
        # The native argument object has no family envelope. Its contents come
        # entirely from the record codec, never a retained raw dictionary.
        encoded = FieldCodec.encode(self)
        del encoded[self.family_discriminator]
        return encoded


@dataclass(frozen=True, kw_only=True)
class PathTool:
    path: str

    def reads(self) -> str:
        return self.path


@dataclass(frozen=True, kw_only=True)
class PatternSearchTool:
    acp_kind = "search"
    pattern: str
    path: str | None = field(
        default=None, metadata={"wire_omit_default": True})

    def reads(self) -> str:
        return self.pattern


@dataclass(frozen=True, kw_only=True)
class CodingTool(ArgumentTool, NativeExecutableTool):
    """Typed Pi fields own mutation paths; the existing SDK executes the tools."""

    claim: FileClaimPath | None = field(init=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "claim", self._parse_resource())

    @abstractmethod
    def _parse_resource(self) -> FileClaimPath | None: ...

    def replacements(self) -> tuple[ReplacementEdit, ...]:
        return ()

    @classmethod
    def create_call(cls, call_id: str, arguments: dict[str, object]):
        from .channel_coding_tools import ExecutableCodingCall

        return ExecutableCodingCall(call_id, cls.parse_arguments(arguments))

    @classmethod
    def native_extension(cls, package: Path) -> Path:
        return package / "dist/channel_coding_tools.mjs"


@dataclass(frozen=True, kw_only=True)
class ReadTool(PathTool, CodingTool):
    acp_kind = "read"
    label = "Read"
    offset: float | None = field(default=None, metadata={"wire_omit_default": True})
    limit: float | None = field(default=None, metadata={"wire_omit_default": True})

    def _parse_resource(self) -> None:
        return None


@dataclass(frozen=True, kw_only=True)
class BashTool(CodingTool):
    acp_kind = "execute"
    label = "Run"
    command: str
    timeout: float | None = field(default=None, metadata={"wire_omit_default": True})

    def reads(self) -> str:
        return self.command

    def _parse_resource(self) -> None:
        return None

    def storage_effects(self, cwd):
        return shell_storage_effects(self.command, cwd)


@dataclass(frozen=True)
class ReplacementEdit:
    old_text: str = field(metadata={"wire_name": "oldText"})
    new_text: str = field(metadata={"wire_name": "newText"})


class MutatingPathTool(PathTool):
    acp_kind = "edit"
    claim_type: ClassVar[type[FileClaimPath]]

    def _parse_resource(self):
        return self.claim_type(Path(self.path))

    def storage_effects(self, cwd):
        return (WriteStorageEffect(physical_path(self.path, cwd)),)


@dataclass(frozen=True, kw_only=True)
class EditTool(MutatingPathTool, CodingTool):
    label = "Edit"
    edits: tuple[ReplacementEdit, ...]

    def __post_init__(self) -> None:
        if not self.edits:
            raise ValueError("Pi edit requires at least one replacement")
        super().__post_init__()

    @classmethod
    def result_diff(cls, result, ok):
        return result.edit_diff(ok)

    def digest(self) -> str:
        return f'{self.declared_name} {self.path}: ' + ' | '.join(
            f'-{one_line(e.old_text, 90)} +{one_line(e.new_text, 140)}' for e in self.edits[:2])

    def replacements(self) -> tuple[ReplacementEdit, ...]:
        return self.edits

    claim_type = ExistingFileClaim


@dataclass(frozen=True, kw_only=True)
class WriteTool(MutatingPathTool, CodingTool):
    label = "Write"
    content: str

    def digest(self) -> str:
        return f'{self.declared_name} {self.path}: {one_line(self.content, 160)}'

    def replacements(self) -> tuple[ReplacementEdit, ...]:
        return (ReplacementEdit(old_text='', new_text=self.content),)

    claim_type = WritableFileClaim


@dataclass(frozen=True, kw_only=True)
class FilePatchTool(PathTool, ToolDeclaration):
    """A file change known by its path and diff alone (Codex's patch), without replacement text."""
    acp_kind = "edit"
    label = "Edit"


class GrepTool(PatternSearchTool, ToolDeclaration):
    label = "Search"


class GlobTool(PatternSearchTool, ToolDeclaration):
    label = "Find"
