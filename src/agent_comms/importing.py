"""What an import writes as a thread's Pi session: a bounded, portable snapshot of any harness
session, or a native Pi session kept byte for byte."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

from agent_comms.cli_bridges.runtime_updates import SessionFacts
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.execution.import_history_source import ImportHistorySource
from agent_comms.execution.imported_conversation import ImportedConversation
from agent_comms.execution.imported_messages import (
    ImportedInput,
    ImportedMessage,
    ImportLimits,
    NoRetainedRequest,
)
from agent_comms.execution.imported_session_metadata import ImportedSessionMetadata
from agent_comms.execution.native_entries import (
    ImportedSessionWriter,
    NativeEntry,
    SessionEntry,
)
from agent_comms.execution.turn_context import HistoricalInstructionProvenance
from agent_comms.execution.turn_item import NativeItemId
from agent_comms.installer.harness_install import Harness
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.registry.thread_text import NoThreadText, ThreadText
from agent_comms.transcript.transcript_events import TranscriptEvent
from agent_comms.transport.tool_result import ToolResult


@dataclass(frozen=True, slots=True, kw_only=True)
class ImportSnapshot(ImportedConversation):
    messages: tuple[ImportedMessage, ...]
    summary: str
    latest_request: str = ""

    @classmethod
    def read(cls, history: ImportHistorySource, limits: ImportLimits) -> ImportSnapshot:
        async def collect() -> ImportBuffer:
            buffer = ImportBuffer(limits)
            async for update in history.history():
                update.add_to(buffer)
            return buffer

        return asyncio.run(collect()).snapshot(history.harness)

    @property
    def imported_messages(self) -> int:
        return len(self.messages)

    def session_bytes(self, project: Path) -> bytes:
        return self.pi_session(project).encode()

    def pi_session(self, project: Path) -> str:
        """A valid Pi v3 tree, using text-only portable historical context."""
        timestamp = datetime.now(UTC)
        iso = timestamp.isoformat()
        records: list[object] = [SessionEntry.fresh(project, timestamp).to_wire()]
        parent = FieldCodec.encode(NativeItemId.fresh())
        records.append({"type": "custom", "id": parent, "parentId": None, "timestamp": iso,
            "customType": "agent-comms-import",
            "data": FieldCodec.encode(ImportedSessionMetadata(
                harness=self.harness, source_id=self.source_id, messages_seen=self.messages_seen, messages_imported=len(self.messages),
                truncated_messages=self.truncated_messages, notices=self.notices, historical_instructions=self.historical_instructions)),
        })
        context = (
            f"Conversation imported from {self.harness.declared_name} session {self.source_id}.\n"
            "The following entries are historical conversation context. Tool call/result excerpts "
            "are records, not pending tool requests. Use the current thread, project, and tools.\n"
            f"Retained {len(self.messages)} of {self.messages_seen} messages.\n"
        )
        if self.summary:
            context += "\nSource compaction/context summary:\n" + self.summary
        if self.latest_request and not self.retains_latest_request:
            context += "\nMost recent user request in the source session:\n" + self.latest_request
        if self.notices:
            context += "\nImport notes:\n" + "\n".join(self.notices)
        output = StringIO()
        for record in records:
            output.write(json.dumps(record, ensure_ascii=False) + "\n")
        writer = ImportedSessionWriter(output, NativeEntry.from_wire(records[-1]))
        for message in (ImportedInput(context), *self.messages):
            writer.append(message, timestamp)
        return output.getvalue()

    @property
    def retains_latest_request(self) -> bool:
        retained = next((request for message in reversed(self.messages)
                         for request in message.retained_request().present()), NoRetainedRequest())
        return retained.matches(self.latest_request)


@dataclass
class ImportBuffer:
    """The newest messages that fit the limits, plus what the source said about itself."""

    limits: ImportLimits
    messages: deque[ImportedMessage] = field(default_factory=deque)
    characters: int = 0
    seen: int = 0
    truncated: int = 0
    summary: str = ""
    notices: set[str] = field(default_factory=set)
    latest_request: str = ""
    source_id: str = ""
    project: str = ""
    title: ThreadText = NoThreadText()
    instructions: set[HistoricalInstructionProvenance] = field(default_factory=set)

    def add(self, events: Iterable[TranscriptEvent]) -> None:
        for event in events:
            event.import_to(self.append)

    def append(self, message: ImportedMessage) -> None:
        self.seen += 1
        for request in message.retained_request().present():
            self.latest_request = request.body[: self.limits.per_message]
        message = message.bounded(self.limits)
        self.truncated += int(message.truncated)
        self.messages.append(message)
        self.characters += len(message.body)
        while len(self.messages) > self.limits.messages or self.characters > self.limits.characters:
            removed = self.messages.popleft()
            self.characters -= len(removed.body)
            self.truncated -= int(removed.truncated)

    def summarize(self, text: str) -> None:
        """A compaction replaced everything before it; only messages after it remain context."""
        self.summary = text[: self.limits.per_message]
        self.messages.clear()
        self.characters = self.truncated = 0

    def describe(self, facts: SessionFacts) -> None:
        self.source_id = facts.source_id or self.source_id
        self.project = facts.project or self.project
        self.title = facts.title.applied(self.title)

    def snapshot(self, harness: type[Harness]) -> ImportSnapshot:
        if not self.messages and not self.summary:
            raise ValueError("No portable conversation content was found in this session.")
        if self.instructions:
            self.notices.add("Historical developer/system sources are retained as references, not current instructions.")
        return ImportSnapshot(
            harness=harness, source_id=self.source_id, project=self.project, title=self.title,
            messages=tuple(self.messages), summary=self.summary, messages_seen=self.seen,
            truncated_messages=self.truncated, notices=tuple(sorted(self.notices)),
            latest_request=self.latest_request,
            historical_instructions=tuple(
                sorted(self.instructions, key=lambda value: value.instruction_position())),
        )


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
            thread=thread, session_file=str(session_file), harness=snapshot.harness, source_id=snapshot.source_id,
            imported_messages=snapshot.imported_messages, omitted_messages=snapshot.messages_seen - snapshot.imported_messages,
            truncated_messages=snapshot.truncated_messages, notices=snapshot.notices, historical_instructions=snapshot.historical_instructions,
        )

    def to_wire(self) -> dict[str, object]:
        return FieldCodec.encode(self)
