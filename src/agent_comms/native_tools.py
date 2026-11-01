"""Typed Pi argument records, native execution capabilities and cooperative claims."""

from __future__ import annotations

from agent_comms.nominal.field_codec import ArgumentsExclude

import json
from abc import abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar

from .envelope_claim_transitions import ExistingFileClaim, FileClaimPath, WritableFileClaim
from .nominal.field_codec import (
    FieldCodec,
    NullMember,
    ToolCallId,
    WireContext,
    WireOmitDefault,
)
from .native_tool_call import CodingCall
from .pi_payloads import PiReported
from .recorded_time import EventTime
from .rule_deal import ReadSpan
from .shell_effects import WriteStorageEffect, physical_path, shell_storage_effects
from .tool_declaration import (
    EditAction,
    EditCall,
    ExecuteAction,
    GlobalCommsExtension,
    ReadAction,
    ReplacementEdit,
    SearchAction,
    ToolDeclaration,
    one_line,
)
from .tool_denial import SelectedToolDenied

if TYPE_CHECKING:
    from .pi_payloads import PiToolResult
    from .pi_rule_gate import PiRuleGate
    from .turn_goal_permission import TurnGoalPermission


@dataclass(frozen=True, slots=True)
class UnsuppliedArgument(NullMember):
    """The model left this optional tool argument out; Pi applies its own default."""

    def value_or[D](self, default: D) -> D:
        return default


@dataclass(frozen=True)
class SuppliedLine(PiReported[float]):
    """A line number or line count the model supplied to Pi's read."""

    value: float


class NativeToolDeclaration(ToolDeclaration):
    """A declared tool participating in the existing native owner check."""

    @classmethod
    @abstractmethod
    def create_call(cls, call_id: ToolCallId, arguments: dict[str, object]) -> CodingCall: ...

    @classmethod
    @abstractmethod
    def native_extension(cls, package: Path) -> Path: ...

    @classmethod
    def extension_arguments(cls, package: Path) -> tuple[str, ...]:
        """Load the declared extensions independently of input/tool authority."""
        declarations = cls.members_with(cls)
        extensions = tuple(dict.fromkeys(tool.native_extension(package) for tool in declarations))
        if any(not extension.is_file() for extension in extensions):
            raise SelectedToolDenied("Native tool extension is not installed")
        return tuple(argument for extension in extensions for argument in ("-e", str(extension)))

    @classmethod
    def from_call(cls, name: str, arguments: object, *, call_id: ToolCallId) -> CodingCall:
        decoded = FieldCodec.decode(dict[str, object], arguments)
        return cls.member_named(name).create_call(call_id, decoded)


@dataclass(frozen=True)
class NativeExecutableTool(NativeToolDeclaration, CodingCall):
    """A decoded tool is the live call; runtime fields are not model arguments."""

    call_id: Annotated[ToolCallId, WireContext()]
    claim: ClassVar[None] = None

    def allowed_claim(self) -> FileClaimPath | None:
        return self.claim

    @classmethod
    def create_call(cls, call_id: ToolCallId, arguments: dict[str, object]) -> NativeExecutableTool:
        return cls.parse_arguments(arguments, call_id=call_id)




@dataclass(frozen=True, kw_only=True)
class CoordinationCall(GlobalCommsExtension, NativeExecutableTool, declared_name=None):
    """Core's CLI owns argument decoding; this call owns identity and live events."""

    declared_name: str
    arguments: dict[str, object]

    def published_goal(self, result: PiToolResult, permission: TurnGoalPermission) -> None:
        result.published_goal(permission)

    def reads(self) -> str:
        return json.dumps(self.arguments)


class CommunicationCall(CoordinationCall, declared_name=None):
    """Ordinary communication stays open during a read deal; execution still checks its owner."""

    def judge_under(self, gate: PiRuleGate) -> None:
        return


class ArgumentTool:
    """One declared execution record supplies validation and native serialization."""

    @property
    def arguments(self) -> dict[str, object]:
        # The native argument object has no family envelope. Its contents come
        # entirely from the record codec, never a retained raw dictionary.
        encoded = FieldCodec.project(self, ArgumentsExclude)
        del encoded[self.family_discriminator]
        return encoded


@dataclass(frozen=True, kw_only=True)
class PathTool:
    path: str

    def reads(self) -> str:
        return self.path


@dataclass(frozen=True, kw_only=True)
class PatternSearchTool(SearchAction):
    pattern: str
    path: Annotated[str | UnsuppliedArgument, WireOmitDefault()] = UnsuppliedArgument()

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
    def native_extension(cls, package: Path) -> Path:
        return package / "dist/channel_coding_tools.mjs"


@dataclass(frozen=True, kw_only=True)
class ReadTool(ReadAction, PathTool, CodingTool):
    label = "Read"
    offset: Annotated[SuppliedLine | UnsuppliedArgument, WireOmitDefault()] = UnsuppliedArgument()
    limit: Annotated[SuppliedLine | UnsuppliedArgument, WireOmitDefault()] = UnsuppliedArgument()
    max_lines: ClassVar[int] = 2_000
    max_bytes: ClassVar[int] = 50 * 1024
    """Pi's read truncates at DEFAULT_MAX_LINES and DEFAULT_MAX_BYTES (dist/core/tools/truncate.js),
    the byte cap applying after a supplied limit too; test_pi_rule_gate checks every dealt file
    fits one default read."""

    def _parse_resource(self) -> None:
        return None

    @classmethod
    def default_span(cls) -> ReadSpan:
        """What a read with neither offset nor limit returns."""
        return ReadSpan(1, cls.max_lines, cls.max_bytes)

    def span(self) -> ReadSpan:
        """An argument the model left out takes Pi's default."""
        return ReadSpan(int(self.offset.value_or(1)), int(self.limit.value_or(self.max_lines)),
                        self.max_bytes)

    def judge_under(self, gate: PiRuleGate) -> None:
        gate.judge_read(self.declared_name, physical_path(self.path, gate.worktree))

    def completed_under(self, gate: PiRuleGate) -> None:
        gate.record_read(physical_path(self.path, gate.worktree), self.span())

    def context_read_paths(self) -> tuple[str, ...]:
        return (self.path,)


@dataclass(frozen=True, kw_only=True)
class BashTool(ExecuteAction, CodingTool):
    label = "Run"
    command: str
    timeout: Annotated[float | UnsuppliedArgument, WireOmitDefault()] = UnsuppliedArgument()

    def reads(self) -> str:
        return self.command

    def _parse_resource(self) -> None:
        return None

    def storage_effects(self, cwd):
        return shell_storage_effects(self.command, cwd)


class MutatingPathTool(EditAction, PathTool):
    claim_type: ClassVar[type[FileClaimPath]]
    declared_name: ClassVar[str]

    def judge_under(self, gate: PiRuleGate) -> None:
        gate.judge_write(self.declared_name, physical_path(self.path, gate.worktree))

    def completed_under(self, gate: PiRuleGate) -> None:
        """A completed write of the audit answers rules, and each is asked about once."""
        if physical_path(self.path, gate.worktree) == gate.audit.resolve():
            gate.audit_written()

    def _parse_resource(self):
        return self.claim_type(Path(self.path))

    def storage_effects(self, cwd):
        return (WriteStorageEffect(physical_path(self.path, cwd)),)

    def edit_calls(self, call_id: ToolCallId, at: EventTime, thread: str,
                   problem: str) -> tuple[EditCall, ...]:
        return (EditCall(call_id, at, thread, self.path, self.replacements(), problem),)


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
class FilePatchTool(EditAction, PathTool, ToolDeclaration):
    """A file change known by its path and diff alone (Codex's patch), without replacement text."""
    label = "Edit"


class GrepTool(PatternSearchTool, ToolDeclaration):
    label = "Search"


class GlobTool(PatternSearchTool, ToolDeclaration):
    label = "Find"
