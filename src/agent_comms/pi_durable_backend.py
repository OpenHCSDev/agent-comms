"""Opt-in Pi Durable: existing Pi records, RPC correlation and history owners."""
from __future__ import annotations

import asyncio
import fcntl
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from pydantic import JsonValue

from agent_comms import field_codec

from .agent_backend import (
    AgentBackend,
    CompactionOutcome,
    Compacts,
    RuntimeLocation,
)
from .agent_events import Error
from .locked_store import SQLiteFile
from .external_transcript import ExternalCut
from .native_child import PiChild
from .native_entries import StoredEntry
from .native_session_files import SessionFiles
from .native_transcript import StoredRecord
from .pi_commands import (
    Compact,
    ExactResponse,
    PiCommand,
    SetModel,
)
from .pi_events import AutoRetryEnd, DurablePiEvent, RetryEnd
from .pi_payloads import PiOmitted, PiResponseData
from .pi_vocabulary import ThinkingLevelField
from .transcript_sources import TranscriptSource
from .unstarted_runtime import UnstartedRuntime


@dataclass(frozen=True)
class DurableDatabase(SessionFiles, RuntimeLocation):
    path: str

    def __post_init__(self):
        if not Path(self.path).is_absolute():
            raise ValueError('Durable database path must be absolute')

    def require_forkable(self):
        raise ValueError('Durable database forking is not implemented')

    def history_source(self, root, incarnation):
        return DurableTranscript(self)

    @property
    def session(self):
        return Path(self.path)

    @property
    def writer_lock(self):
        return self.session.with_suffix(self.session.suffix + '.lock')

    @property
    def members(self):
        return (*SQLiteFile.files(self.session), self.writer_lock)

    def storage_files(self):
        return (self,)


@dataclass(frozen=True)
class HistoryData(PiResponseData):
    items: tuple[StoredEntry, ...]
    cursor: dict[str, JsonValue] | None = None


def _history_request(source, command):
    with source.reading() as reader:
        return reader.execute(reader, command)


@dataclass(frozen=True)
class DurableTranscript(TranscriptSource):
    location: DurableDatabase
    execute: Callable = field(default=_history_request, repr=False, compare=False)

    @contextmanager
    def reading(self):
        if not Path(self.location.path).is_file():
            yield replace(self, execute=lambda source, command: HistoryData(()))
            return
        with asyncio.Runner() as loop:
            child = loop.run(PiChild.start_command(('node', str(Path(__file__).with_name('pi_durable_host.mjs'))),
                cwd=str(Path(self.location.path).parent), event_type=DurablePiEvent))
            try:
                loop.run(child.request(OpenHistory(self.location.path)))
                yield replace(self, execute=lambda source, command: loop.run(child.request(command)))
            finally:
                loop.run(child.shutdown())

    def cut(self):
        return ExternalCut(max((entry.id for entry in self.execute(self, Entries(1)).items), default=0))

    def records(self, minimum, maximum, order):
        if maximum < minimum:
            return
        cursor = None
        while True:
            page = self.execute(self, Entries(128, cursor, minimum, maximum, order))
            for entry in page.items:
                yield StoredRecord(entry.id - 1, entry.id, entry, True)
            cursor = page.cursor
            if cursor is None:
                return

    def forward(self, after, through):
        return self.records(after + 1, through, 'ascending')

    def reverse(self, before):
        return self.records(1, before, 'descending')


@dataclass(frozen=True)
class RecoveryData(PiResponseData):
    work: tuple[str, ...]


@dataclass(frozen=True)
class OutcomeData(PiResponseData):
    outcome: CompactionOutcome


class DurableRetryEnd(RetryEnd, DurablePiEvent):
    wire_kind = AutoRetryEnd.wire_kind


@dataclass(frozen=True)
class TaskFailed(DurablePiEvent):
    task_id: Annotated[int, field_codec.WireName("taskId")]
    message: str

    def convert(self):
        return (Error(self.message),)


class HostQuery(ExactResponse, PiCommand, declared_name=None):
    pass


@dataclass(frozen=True)
class Open(HostQuery):
    path: str
    cwd: str
    provider: str | PiOmitted
    model_id: Annotated[str | PiOmitted, field_codec.WireName("modelId")]
    thinking_level: Annotated[ThinkingLevelField, field_codec.WireName("thinkingLevel")]
    auth_path: Annotated[str, field_codec.WireName("authPath")]
    response_payload = RecoveryData


@dataclass(frozen=True)
class OpenHistory(HostQuery):
    path: str


class Watch(HostQuery):
    pass


class Unwatch(HostQuery):
    pass


class Resume(HostQuery):
    pass


class Inspect(HostQuery):
    response_payload = RecoveryData


@dataclass(frozen=True)
class Settle(HostQuery):
    work: str
    disposition: str


@dataclass(frozen=True)
class Entries(HostQuery):
    limit: int
    cursor: dict[str, JsonValue] | None = None
    min_entry_id: Annotated[
        int | PiOmitted, field_codec.WireName("minEntryId"), field_codec.WireOmitDefault()
    ] = PiOmitted()
    max_entry_id: Annotated[
        int | PiOmitted, field_codec.WireName("maxEntryId"), field_codec.WireOmitDefault()
    ] = PiOmitted()
    order: str = "descending"
    response_payload = HistoryData


class CompactDurable(ExactResponse, Compact):
    response_payload = OutcomeData


class PiDurableBackend(AgentBackend, Compacts):
    serves_threads = False
    location_type = DurableDatabase
    unstarted_type = UnstartedRuntime
    event_kinds = frozenset(kind for member in DurablePiEvent.members_with(DurablePiEvent) for kind in member.known_kinds())

    def __init__(self):
        self.child: PiChild | None = None
        self.lock = asyncio.Lock()

    @classmethod
    def for_worker(cls, runner, session_id):
        return cls()

    @classmethod
    def new_location(cls, root, worktree):
        return DurableDatabase(str(Path(root).absolute() / 'durable-sessions' / f'{uuid4().hex}.sqlite3'))

    async def open(self, thread):
        async with self.lock:
            if self.child is None:
                path = Path(thread.runtime.path)
                path.parent.mkdir(parents=True, exist_ok=True)
                with ExitStack() as resources:
                    lock = resources.enter_context(thread.runtime.writer_lock.open('a+b'))
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    child = await PiChild.start_command(('node', str(Path(__file__).with_name('pi_durable_host.mjs'))),
                        cwd=thread.worktree, pass_fds=(lock.fileno(),), event_type=DurablePiEvent, resources=resources.pop_all())
                async with child.failures():
                    model = thread.model.set_model(SetModel)
                    await child.request(Open(str(path), thread.worktree, model.provider, model.model_id, thread.thinking_level, str(Path.home() / '.pi/agent/auth.json')))
                    await child.request(Resume())
                self.child = child
            return self.child

    async def close_idle(self):
        async with self.lock:
            if self.child is not None:
                await self.child.shutdown()
                self.child = None

    async def compact(self, thread, instructions):
        child = await self.open(thread)
        command = CompactDurable(custom_instructions=PiOmitted.from_input(instructions))
        return (await child.request(command)).outcome

