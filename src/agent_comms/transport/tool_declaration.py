"""Shared tool names and presentation; execution capabilities select their own view."""

from __future__ import annotations

import json
from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Annotated, ClassVar, Self

from agent_comms.execution.tool_results import NoToolDiff
from agent_comms.nominal.class_fact import ClassFact
from agent_comms.nominal.declared_family import DeclaredFamily, UnknownFamilyMember
from agent_comms.nominal.field_codec import (
    FieldCodec,
    MalformedRecord,
    NullMember,
    ToolCallId,
    WireName,
)
from agent_comms.storage.recorded_time import EventTime
from agent_comms.transport import tools as tool_declarations
from agent_comms.transport.tool_denial import SelectedToolDenied


class GlobalCommsExtension:
    """Requests and their executing calls load the same packaged extension."""

    @classmethod
    def native_extension(cls, package: Path) -> Path:
        return package / "agent-comms-extensions/global-agent-comms/index.mjs"


def one_line(text: str, limit: int) -> str:
    return " ".join(text.split())[:limit]


@dataclass(frozen=True)
class ReplacementEdit:
    old_text: Annotated[str, WireName("oldText")]
    new_text: Annotated[str, WireName("newText")]


@dataclass(frozen=True)
class EditCall:
    id: ToolCallId
    at: EventTime
    thread: str
    path: str
    hunks: tuple[ReplacementEdit, ...]
    problem: str


class ToolAction:
    """A tool capability, composed beside its declaration family."""

    members: ClassVar[set[type[ToolAction]]] = set()

    def __init_subclass__(cls) -> None:
        super().__init_subclass__()
        if ToolAction in cls.__bases__:
            ToolAction.members.add(cls)


class SearchAction(ToolAction):
    """Find matching information."""


class ReadAction(ToolAction):
    """Read existing content."""


class ExecuteAction(ToolAction):
    """Execute a command."""


class EditAction(ToolAction):
    """Change content."""


class OtherAction(ToolAction):
    """A tool with no more specific action."""


class ContextRows:
    """How a reading tool lays out a file in the model's context: Claude's Read shows each line
    as its number, a tab, then the line. The leading numbered block is the file; numbers that do
    not run consecutively make no line count. Records end only at a physical LF: a line may hold
    U+2028, U+0085 or other characters str.splitlines would split on."""

    def rows(self, text: str) -> tuple[tuple[int, str], ...]:
        rows: list[tuple[int, str]] = []
        for row in text.split('\n'):
            number, tab, line = row.partition('\t')
            if not (tab and number.isdecimal()):
                break
            rows.append((int(number), line))
        consecutive = all(later[0] == earlier[0] + 1
                          for earlier, later in zip(rows, rows[1:], strict=False))
        return tuple(rows) if consecutive else ()


class PlainContextRows(ContextRows):
    """A file printed as it is, as `cat` shows it: its lines in order, numbered by position."""

    def rows(self, text: str) -> tuple[tuple[int, str], ...]:
        return tuple(enumerate(text.split('\n'), start=1))


@dataclass(frozen=True)
class ToolDeclaration(DeclaredFamily, affix="Tool"):
    family_discriminator: ClassVar[str] = "tool"
    label: ClassVar[str]

    @classmethod
    def serving(cls, request: ToolDeclaration) -> Self:
        """This capability's member for the request's wire name, carrying the request's
        fields: what the wire round trip decodes, without the wire. Looking the name up
        acquires the family's members, so a process meets the served module only when it
        first serves a request itself."""
        member = cls.member_named(request.selection_key())
        return member(**{
            field.name: getattr(request, field.name) for field in fields(request) if field.init
        })

    @classmethod
    def declared_action(cls) -> type[ToolAction]:
        """The first action capability in the tool's C3 MRO."""
        return next(
            (action for base in cls.__mro__ for action in ToolAction.members if base is action),
            OtherAction,
        )

    @classmethod
    def _member_declarations(cls):
        """Acquire every tool declaration owner: the transport/tools package's modules."""
        DeclaredFamily.discover_package(tool_declarations)
        return super()._member_declarations()

    @classmethod
    def for_name(cls, name: str) -> type[ToolDeclaration] | UndeclaredTool:
        try:
            return cls.member_named(name.lower())
        except UnknownFamilyMember:
            return UndeclaredTool(name)

    @classmethod
    def parse_arguments(cls, arguments: Mapping[str, object], *, call_id: ToolCallId) -> Self:
        if cls.family_discriminator in arguments:
            raise SelectedToolDenied("Native arguments cannot contain a family selector")
        selected = {cls.family_discriminator: cls.declared_name}
        return FieldCodec.decode_bound(cls, {**selected, **arguments}, call_id=call_id)

    @classmethod
    def record(
        cls, name: str, arguments: object, *, call_id: ToolCallId
    ) -> ToolDeclaration | NoToolRecord:
        """The call decoded once as Core's tool record, or no record when Core does not decode it."""
        try:
            decoded = FieldCodec.decode(dict[str, object], arguments)
            return cls.for_name(name).parse_arguments(
                decoded, call_id=FieldCodec.decode(ToolCallId, call_id))
        except MalformedRecord, SelectedToolDenied:
            return NoToolRecord()

    def recorded_call(self, shown):
        """The shown call, carrying this record."""
        return shown.recorded(self)

    def edit_calls(
        self, call_id: ToolCallId, at: EventTime, thread: str, problem: str
    ) -> tuple[EditCall, ...]:
        return ()

    def reads(self) -> str:
        """What the call names after its label: nothing unless the tool says."""
        return ""

    def context_read_paths(self) -> tuple[str, ...]:
        """Files whose visible text the call puts in the model's context: none unless the tool
        reads one."""
        return ()

    context_rows: ClassVar[ContextRows] = ContextRows()
    """How those files' lines appear in the call's result."""

    def digest(self) -> str:
        """What the call does in one line, enough to judge it."""
        return f"{self.declared_name} {one_line(self.reads(), 260)}"

    def call_digest(self, name: str, arguments: object) -> str:
        """A call in one line: its record's digest."""
        return self.digest()

    @classmethod
    def caption(cls, name: str, tool: ToolDeclaration | NoToolRecord) -> str:
        """A call reads as its label, then what its record names; with no record, its label."""
        reads = one_line(text := tool.reads(), len(text))
        return cls.for_name(name).label + (" " + reads if reads else "")

    @classmethod
    def result_diff(cls, result, ok):
        """Opaque metadata cannot invent edit evidence."""
        return NoToolDiff()


class NativeRequestTools(GlobalCommsExtension, ToolDeclaration, declared_name=None):
    """Schema-bearing request declarations form the native catalog."""

    @classmethod
    def loads_tools(cls) -> bool:
        return False

    @classmethod
    @abstractmethod
    def schema(cls) -> dict[str, object]:
        """The declaration supplies the schema exposed to native clients."""

    @ClassFact
    def catalog(cls) -> tuple[dict[str, object], ...]:
        """Every member's schema. A schema says only what its declaration says, so the catalog
        is derived once for the declarations that exist, never again for each launch."""
        return tuple(tool.schema() for tool in cls.members_with(cls))


@dataclass(frozen=True)
class NoToolRecord(NullMember):
    """Core did not decode the call into a tool record; its raw arguments stand."""

    def reads(self) -> str:
        return ""

    def edit_calls(
        self, call_id: ToolCallId, at: EventTime, thread: str, problem: str
    ) -> tuple[EditCall, ...]:
        return ()

    def call_digest(self, name: str, arguments: object) -> str:
        return f"{name} {one_line(json.dumps(arguments), 260)}"

    def recorded_call(self, shown):
        return shown


@dataclass(frozen=True)
class UndeclaredTool(ToolDeclaration, declared_name=None):
    """An external name is visible as supplied, without becoming a declaration."""

    name: str

    @property
    def label(self) -> str:
        return self.name
