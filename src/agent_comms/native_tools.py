"""Native tool declarations own presentation and cooperative coding capability.

Pi owns execution and full argument schemas. Unknown extension tools remain
presentable, without acquiring the CodingTool admission capability.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from .tool_declaration import ToolDeclaration
from .envelope_claim_transitions import ExistingFileClaim, FileClaimPath, WritableFileClaim
from .field_codec import FieldCodec

class NativeExecutableTool(ToolDeclaration):
    """A declared tool whose calls participate in the existing native owner check."""

    claim: ClassVar[None] = None

    @classmethod
    @abstractmethod
    def create_call(cls, call_id: str, arguments: dict[str, Any]): ...

    @classmethod
    @abstractmethod
    def native_extension(cls, package: Path) -> Path: ...

    @classmethod
    def from_call(cls, name: str, arguments: dict[str, Any], *, call_id: str):
        cls._member_declarations()
        FieldCodec.encode(arguments)
        return cls.decode(name).create_call(call_id, arguments)


class NativeRequestTools:
    """Core requests retain their exact native payload; the existing CLI executes them."""

    @classmethod
    def create_call(cls, call_id: str, arguments: dict[str, Any]):
        from .channel_coding_tools import CoordinationCall

        return CoordinationCall(call_id, cls, arguments)

    @classmethod
    def native_extension(cls, package: Path) -> Path:
        return package / "agent-comms-extensions/global-agent-comms/index.mjs"


class PathTool:
    @classmethod
    def detail(cls, arguments: dict[str, Any]) -> object:
        return arguments.get("path") or arguments.get("file_path")


@dataclass(frozen=True)
class CodingTool(NativeExecutableTool):
    """Pi owns argument schemas; this family owns only cooperative claim behavior."""

    arguments: dict[str, Any]
    claim: FileClaimPath | None = field(init=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "claim", self._parse_resource())

    @abstractmethod
    def _parse_resource(self) -> FileClaimPath | None: ...

    @classmethod
    def create_call(cls, call_id: str, arguments: dict[str, Any]):
        from .channel_coding_tools import CodingCall

        return CodingCall(call_id, cls(arguments))

    @classmethod
    def native_extension(cls, package: Path) -> Path:
        return package / "dist/channel_coding_tools.mjs"


class ReadTool(PathTool, CodingTool):
    acp_kind = "read"

    def _parse_resource(self) -> None:
        return None


class BashTool(CodingTool):
    acp_kind = "execute"
    label = "Run"

    @classmethod
    def detail(cls, arguments: dict[str, Any]) -> object:
        return arguments.get("command")

    def _parse_resource(self) -> None:
        return None


class EditTool(PathTool, CodingTool):
    acp_kind = "edit"

    @classmethod
    def result_diff(cls, result, ok):
        return result.edit_diff(ok)

    def _parse_resource(self) -> FileClaimPath:
        return ExistingFileClaim(Path(self.arguments["path"]))


class WriteTool(PathTool, CodingTool):
    acp_kind = "edit"

    def _parse_resource(self) -> FileClaimPath:
        return WritableFileClaim(Path(self.arguments["path"]))


class GrepTool(ToolDeclaration):
    acp_kind = "search"
    label = "Search"

    @classmethod
    def detail(cls, arguments: dict[str, Any]) -> object:
        pattern = arguments.get("pattern")
        path = arguments.get("path")
        return f"{pattern} in {path}" if pattern and path else pattern or path


class GlobTool(ToolDeclaration):
    acp_kind = "search"
    label = "Find"

    @classmethod
    def detail(cls, arguments: dict[str, Any]) -> object:
        return arguments.get("pattern") or arguments.get("path")
