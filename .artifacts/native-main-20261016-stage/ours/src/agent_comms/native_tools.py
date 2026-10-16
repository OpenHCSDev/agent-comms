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


class NativeExecutableTool(ToolDeclaration):
    """A declared tool participating in the existing native owner check."""

    claim: ClassVar[None] = None

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


class ArgumentTool(ToolDeclaration):
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

    @property
    @abstractmethod
    def detail(self) -> str: ...


@dataclass(frozen=True, kw_only=True)
class PathTool:
    path: str

    @property
    def detail(self) -> str:
        return self.path


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

    @property
    def detail(self) -> str:
        return self.command

    def _parse_resource(self) -> None:
        return None


@dataclass(frozen=True)
class ReplacementEdit:
    old_text: str = field(metadata={"wire_name": "oldText"})
    new_text: str = field(metadata={"wire_name": "newText"})


@dataclass(frozen=True, kw_only=True)
class EditTool(PathTool, CodingTool):
    acp_kind = "edit"
    label = "Edit"
    edits: tuple[ReplacementEdit, ...]

    def __post_init__(self) -> None:
        if not self.edits:
            raise ValueError("Pi edit requires at least one replacement")
        super().__post_init__()

    @classmethod
    def result_diff(cls, result, ok):
        return result.edit_diff(ok)

    def _parse_resource(self) -> FileClaimPath:
        return ExistingFileClaim(Path(self.path))


@dataclass(frozen=True, kw_only=True)
class WriteTool(PathTool, CodingTool):
    acp_kind = "edit"
    label = "Write"
    content: str

    def _parse_resource(self) -> FileClaimPath:
        return WritableFileClaim(Path(self.path))


class GrepTool(ToolDeclaration):
    acp_kind = "search"
    label = "Search"


class GlobTool(ToolDeclaration):
    acp_kind = "search"
    label = "Find"
