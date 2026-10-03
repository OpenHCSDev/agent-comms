"""Native tool declarations own presentation and cooperative coding capability.

Pi owns execution and full argument schemas. Unknown extension tools remain
presentable, without acquiring the CodingTool admission capability.
"""

from __future__ import annotations

import json
from abc import abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, TYPE_CHECKING

from .agent_events import ToolStart
from .declared_family import DeclaredFamily
from .envelope_claim_transitions import ExistingFileClaim, FileClaimPath, WritableFileClaim
from .field_codec import FieldCodec

if TYPE_CHECKING:
    from .pi_payloads import ToolCallContent


class NativeTool(DeclaredFamily, affix="Tool"):
    acp_kind: ClassVar[str] = "other"
    action: ClassVar[str] = ""

    @classmethod
    def for_name(cls, name: str) -> type[NativeTool]:
        try:
            return cls.decode(name.lower())
        except ValueError:
            return cls

    @classmethod
    def start(cls, call_id: str, name: str, arguments: dict[str, Any]) -> ToolStart:
        declaration = cls.for_name(name)
        detail = declaration.detail(arguments)
        action = declaration.action or name.replace("_", " ").title()
        if detail:
            try:
                text = detail if isinstance(detail, str) else json.dumps(detail)
            except (TypeError, ValueError):
                text = str(detail)
            text = text.strip()
            action += " " + text[:120] + ("…" if len(text) > 120 else "")
        return ToolStart(call_id, name, action, arguments, declaration.acp_kind)

    @classmethod
    def detail(cls, arguments: dict[str, Any]) -> object:
        return None

    @classmethod
    def result_diff(cls, result, ok):
        """Unknown tool metadata remains opaque; it cannot invent edit evidence."""
        return None

    @classmethod
    def result_artifacts(cls, result, ok):
        return ()


class PathTool:
    @classmethod
    def detail(cls, arguments: dict[str, Any]) -> object:
        return arguments.get("path") or arguments.get("file_path")


@dataclass(frozen=True)
class CodingTool(NativeTool):
    """Pi owns argument schemas; this family owns only cooperative claim behavior."""

    arguments: dict[str, Any]
    claim: FileClaimPath | None = field(init=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "claim", self._parse_resource())

    @abstractmethod
    def _parse_resource(self) -> FileClaimPath | None: ...

    def matches_request(self, request: ToolCallContent) -> bool:
        """Exact intended invocation, independent of call ID or execution success."""
        return request.name == self.declared_name and request.arguments == self.arguments

    @classmethod
    def from_call(cls, name: str, arguments: dict[str, Any]) -> CodingTool:
        # The native tool validates its full schema. Preserve it exactly for
        # event/socket correlation, without maintaining a second Pi schema.
        FieldCodec.encode(arguments)
        return cls.decode(name)(arguments)


class ReadTool(PathTool, CodingTool):
    acp_kind = "read"

    def _parse_resource(self) -> None:
        return None


class BashTool(CodingTool):
    acp_kind = "execute"
    action = "Run"

    @classmethod
    def detail(cls, arguments: dict[str, Any]) -> object:
        return arguments.get("command")

    def _parse_resource(self) -> None:
        return None


class FileMutationTool:
    @classmethod
    def result_artifacts(cls, result, ok):
        return result.artifacts(ok)


class EditTool(FileMutationTool, PathTool, CodingTool):
    acp_kind = "edit"

    @classmethod
    def result_diff(cls, result, ok):
        return result.edit_diff(ok)

    def _parse_resource(self) -> FileClaimPath:
        return ExistingFileClaim(Path(self.arguments["path"]))


class WriteTool(FileMutationTool, PathTool, CodingTool):
    acp_kind = "edit"

    def _parse_resource(self) -> FileClaimPath:
        return WritableFileClaim(Path(self.arguments["path"]))


class GrepTool(NativeTool):
    acp_kind = "search"
    action = "Search"

    @classmethod
    def detail(cls, arguments: dict[str, Any]) -> object:
        pattern = arguments.get("pattern")
        path = arguments.get("path")
        return f"{pattern} in {path}" if pattern and path else pattern or path


class GlobTool(NativeTool):
    acp_kind = "search"
    action = "Find"

    @classmethod
    def detail(cls, arguments: dict[str, Any]) -> object:
        return arguments.get("pattern") or arguments.get("path")
