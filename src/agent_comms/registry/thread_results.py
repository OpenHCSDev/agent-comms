"""Client-facing wire results and import directory choices for thread operations.

Serving and registry mutation live in agent_comms.registry.thread_management and
agent_comms.importing so remote clients decode these records without importing bridges
or store writers.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path

from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.execution.imported_conversation import ImportedConversation
from agent_comms.execution.context_provenance import HistoricalInstructionProvenance
from agent_comms.installer.harness_install import Harness
from agent_comms.nominal.field_codec import (
    FieldCodec,
    NullMember,
    SingleFieldValue,
    SubclassUnion,
)
from agent_comms.transport.tool_result import ToolResult


@dataclass(frozen=True, slots=True)
class ChangeResult(ToolResult):
    """A value that moved from previous to current; changed says whether it differs."""

    previous: str
    current: str
    changed: bool


@dataclass(frozen=True, slots=True)
class RenameThreadResult(ChangeResult):
    """A thread display name change."""


@dataclass(frozen=True, slots=True)
class ProjectChangeResult(ChangeResult):
    thread: str


@dataclass(frozen=True, slots=True)
class ThreadTitleResult(ToolResult):
    """A title set on its own; the thread's identity and aliases are untouched."""

    name: str
    title: str


class ImportDirectory(SubclassUnion, affix="Directory"):
    """Where an imported thread works: a given directory, or its source's project."""

    @abstractmethod
    def project(self, snapshot: ImportedConversation) -> str: ...

    @abstractmethod
    def local_directory(self) -> Path:
        """The directory a running harness's session is asked for."""


@dataclass(frozen=True, slots=True)
class GivenDirectory(SingleFieldValue, ImportDirectory):
    path: str

    def project(self, snapshot: ImportedConversation) -> str:
        return self.path

    def local_directory(self) -> Path:
        return Path(self.path).expanduser()


@dataclass(frozen=True, slots=True)
class SourceDirectory(NullMember, ImportDirectory):
    def project(self, snapshot: ImportedConversation) -> str:
        return snapshot.project

    def local_directory(self) -> Path:
        return Path.cwd()


@dataclass(frozen=True, slots=True)
class ThreadImported(CliResult, ToolResult):
    thread: str
    session_file: str
    harness: type[Harness]
    source_id: str
    imported_messages: int
    omitted_messages: int
    truncated_messages: int
    notices: tuple[str, ...]
    historical_instructions: tuple[HistoricalInstructionProvenance, ...] = ()

    @classmethod
    def of(cls, thread: str, session_file: Path, snapshot: ImportedConversation) -> ThreadImported:
        return cls(
            thread=thread,
            session_file=str(session_file),
            harness=snapshot.harness,
            source_id=snapshot.source_id,
            imported_messages=snapshot.imported_messages,
            omitted_messages=snapshot.messages_seen - snapshot.imported_messages,
            truncated_messages=snapshot.truncated_messages,
            notices=snapshot.notices,
            historical_instructions=snapshot.historical_instructions,
        )

    def to_wire(self) -> dict[str, object]:
        return FieldCodec.encode(self)
