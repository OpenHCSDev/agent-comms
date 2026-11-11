"""Typed Pi argument records, native execution capabilities and cooperative claims."""

from __future__ import annotations

import json
from abc import abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar

from agent_comms.execution.pi_wire import PiReported
from agent_comms.messaging.envelope_claim_transitions import (
    ExistingFileClaim,
    FileClaimPath,
    WritableFileClaim,
)
from agent_comms.nominal.declared_family import UnknownFamilyMember
from agent_comms.nominal.field_codec import (
    FieldCodec,
    MalformedRecord,
    NullMember,
    ToolCallId,
    WireContext,
    WireOmitDefault,
)
from agent_comms.projects.shell_effects import (
    ShellOutput,
    WriteStorageEffect,
    physical_path,
    shell_storage_effects,
)
from agent_comms.registry.thread_identity import OfferedTools, UnobservedTools
from agent_comms.storage.recorded_time import EventTime
from agent_comms.team.rule_deal import ReadSpan
from agent_comms.transport.native_tool_call import CodingCall, RefusedCodingCall
from agent_comms.transport.request import ArgumentTool
from agent_comms.transport.tool_declaration import (
    ContextRows,
    EditAction,
    EditCall,
    ExecuteAction,
    GlobalCommsExtension,
    PlainContextRows,
    ReadAction,
    ReplacementEdit,
    SearchAction,
    ToolDeclaration,
    one_line,
)
from agent_comms.transport.tool_denial import SelectedToolDenied

if TYPE_CHECKING:
    from agent_comms.execution.turn_goal_permission import TurnGoalPermission
    from agent_comms.pi_payloads import PiToolResult
    from agent_comms.team.pi_rule_gate import PiRuleGate


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
    def decode(cls, call_id: object, name: object, arguments: object,
               offered: OfferedTools = UnobservedTools()) -> CodingCall:
        """``offered`` is the thread's catalogue for this turn; without one only declared
        members decode."""
        try:
            call_id = FieldCodec.decode(ToolCallId, call_id)
        except MalformedRecord as error:
            raise SelectedToolDenied("Malformed native coding call identity") from error
        if type(name) is not str:
            raise SelectedToolDenied("Malformed native coding call identity")
        try:
            return cls.from_call(name, arguments, call_id=call_id, offered=offered)
        # A name no member declares is refused data too: answering "no" here keeps the
        # call from dying with the socket, which Pi can only report as outcome UNKNOWN.
        except (MalformedRecord, UnknownFamilyMember) as error:
            return RefusedCodingCall(call_id, name, arguments, str(error))

    @classmethod
    def from_call(cls, name: str, arguments: object, *, call_id: ToolCallId,
                  offered: OfferedTools) -> CodingCall:
        """A declared member's call, or else a tool this turn was offered by name."""
        decoded = FieldCodec.decode(dict[str, object], arguments)
        try:
            member = cls.member_named(name)
        except UnknownFamilyMember:
            return McpToolCall(call_id=call_id, declared_name=offered.tool_named(name).value,
                               arguments=decoded)
        return member.create_call(call_id, decoded)


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
class NamedArgumentsCall(NativeExecutableTool, declared_name=None):
    """A call whose name is data rather than a declared member, carrying its arguments whole
    for whatever executes it to decode."""

    declared_name: str
    arguments: dict[str, object]

    def reads(self) -> str:
        return json.dumps(self.arguments)


@dataclass(frozen=True, kw_only=True)
class CoordinationCall(GlobalCommsExtension, NamedArgumentsCall, declared_name=None):
    """Core's CLI owns argument decoding; this call owns identity and live events."""

    def published_goal(self, result: PiToolResult, permission: TurnGoalPermission) -> None:
        result.published_goal(permission)


class CommunicationCall(CoordinationCall, declared_name=None):
    """Ordinary communication stays open during a read deal; execution still checks its owner."""

    def judge_under(self, gate: PiRuleGate) -> None:
        return


@dataclass(frozen=True, kw_only=True)
class McpToolCall(NamedArgumentsCall, declared_name=None):
    """A tool pi-mcp-client registered for this turn: one per connected MCP server's tool,
    plus its catalogue, resource and prompt tools. The thread's offered catalogue holds the
    name. These tools touch no worktree path, so the owner grants no file claim."""

    @classmethod
    def native_extension(cls, package: Path) -> Path:
        return package / "agent-comms-extensions/pi-mcp-client/index.mjs"


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

    context_rows: ClassVar[ContextRows] = PlainContextRows()

    def context_read_paths(self) -> tuple[str, ...]:
        """A command that is exactly `cat` of one file puts that file in context as it is; any
        other command's output is its own text, never a file's."""
        return ShellOutput.of(self.command).context_files()

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
