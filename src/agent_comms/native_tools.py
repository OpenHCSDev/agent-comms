"""Typed Pi argument records, native execution capabilities and cooperative claims."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from .tool_declaration import ToolDeclaration
from .envelope_claim_transitions import ExistingFileClaim, FileClaimPath, WritableFileClaim
from .field_codec import FieldCodec
from .native_tool_call import SelectedToolDenied
from .storage_rules import WriteStorageEffect, physical_path, shell_storage_effects


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
    def from_call(cls, name: str, arguments: dict[str, object], *, call_id: str):
        cls._member_declarations()
        FieldCodec.encode(arguments)
        return cls.decode(name).create_call(call_id, arguments)


class NativeRequestTools:
    """Core's existing CLI decodes and executes its declared request fields."""

    @classmethod
    def create_call(cls, call_id: str, arguments: dict[str, object]):
        from .channel_coding_tools import CoordinationCall

        return CoordinationCall(call_id, cls, arguments)

    @classmethod
    def native_extension(cls, package: Path) -> Path:
        return package / "agent-comms-extensions/global-agent-comms/index.mjs"


class ArgumentTool:
    """One declared execution record supplies validation and native serialization."""

    @classmethod
    def parse_arguments(cls, arguments: dict[str, object]):
        if cls.family_discriminator in arguments:
            raise SelectedToolDenied("Native arguments cannot contain a family selector")
        try:
            return FieldCodec.decode(cls, {cls.family_discriminator: cls.declared_name, **arguments})
        except (TypeError, ValueError, KeyError) as error:
            raise SelectedToolDenied(f"Invalid {cls.declared_name} arguments: {error}") from error

    @property
    def arguments(self) -> dict[str, object]:
        # The native argument object has no family envelope. Its contents come
        # entirely from the record codec, never a retained raw dictionary.
        encoded = FieldCodec.encode(self)
        del encoded[self.family_discriminator]
        return encoded


@dataclass(frozen=True, kw_only=True)
class PathTool:
    preview_fields: ClassVar[tuple[str, ...]] = ('path',)
    path: str


class PatternSearchTool:
    acp_kind = "search"
    preview_fields: ClassVar[tuple[str, ...]] = ('pattern',)


@dataclass(frozen=True, kw_only=True)
class CodingTool(ArgumentTool, NativeExecutableTool):
    """Typed Pi fields own mutation paths; the existing SDK executes the tools."""

    claim: FileClaimPath | None = field(init=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "claim", self._parse_resource())

    @abstractmethod
    def _parse_resource(self) -> FileClaimPath | None: ...

    @classmethod
    def create_call(cls, call_id: str, arguments: dict[str, object]):
        from .channel_coding_tools import CodingCall

        return CodingCall(call_id, cls.parse_arguments(arguments))

    @classmethod
    def native_extension(cls, package: Path) -> Path:
        return package / "dist/channel_coding_tools.mjs"


@dataclass(frozen=True, kw_only=True)
class ReadTool(PathTool, CodingTool):
    acp_kind = "read"
    label = "Read"
    offset: float | None = field(default=None, metadata={"wire_omit_default": True, "wire_nonnull": True})
    limit: float | None = field(default=None, metadata={"wire_omit_default": True, "wire_nonnull": True})

    def _parse_resource(self) -> None:
        return None


@dataclass(frozen=True, kw_only=True)
class BashTool(CodingTool):
    acp_kind = "execute"
    label = "Run"
    command: str
    timeout: float | None = field(default=None, metadata={"wire_omit_default": True, "wire_nonnull": True})

    preview_fields = ('command',)

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

    claim_type = ExistingFileClaim


@dataclass(frozen=True, kw_only=True)
class WriteTool(MutatingPathTool, CodingTool):
    label = "Write"
    content: str

    claim_type = WritableFileClaim


class GrepTool(PatternSearchTool, ToolDeclaration):
    label = "Search"


class GlobTool(PatternSearchTool, ToolDeclaration):
    label = "Find"
