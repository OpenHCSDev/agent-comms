"""Opt-in Pi Durable: existing Pi records, RPC correlation and history owners."""
from __future__ import annotations

import asyncio
import fcntl
from contextlib import ExitStack, contextmanager
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from uuid import uuid4

from pydantic import JsonValue

from .agent_backend import AgentBackend, Compacts, CompactionOutcome, ConfiguresModel, ReportsContext, RuntimeLocation
from .context_reading import ContextReading
from .agent_events import Error
from .coordination_database import CoordinationStore
from .native_child import PiChild
from .external_transcript import ExternalCut
from .native_entries import StoredEntry
from .native_session_files import SessionFiles
from .native_transcript import StoredRecord
from .pi_commands import Compact, GetAvailableModels, GetSessionStats, GetState, PiCommand, SetModel, SetThinkingLevel
from .pi_events import AutoRetryEnd, DurablePiEvent, RetryEnd
from .pi_payloads import PiResponseData
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
        return (*CoordinationStore.files(self.session), self.writer_lock)

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
    task_id: int = field(metadata={'wire_name': 'taskId'})
    message: str

    def convert(self):
        return (Error(self.message),)


class HostQuery(PiCommand, declared_name=None):
    strict_response = True


@dataclass(frozen=True)
class Open(HostQuery):
    path: str
    cwd: str
    provider: str | None
    model_id: str | None = field(metadata={'wire_name': 'modelId'})
    thinking_level: ThinkingLevelField = field(metadata={'wire_name': 'thinkingLevel'})
    auth_path: str = field(metadata={'wire_name': 'authPath'})
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
    min_entry_id: int | None = field(default=None, metadata={'wire_name': 'minEntryId', 'wire_omit_default': True})
    max_entry_id: int | None = field(default=None, metadata={'wire_name': 'maxEntryId', 'wire_omit_default': True})
    order: str = 'descending'
    response_payload = HistoryData


class CompactDurable(Compact):
    strict_response = True
    response_payload = OutcomeData


class PiDurableBackend(AgentBackend, Compacts, ReportsContext, ConfiguresModel):
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
                    model = SetModel.from_name(thread.model)
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
        return (await child.request(CompactDurable(custom_instructions=instructions))).outcome

    async def context_usage(self, thread) -> ContextReading:
        child = await self.open(thread)
        usage = (await child.request(GetSessionStats())).context_usage
        if usage is None:
            raise ValueError('Durable session has no context observation')
        return usage.context_window.reading(usage.token_total)

    async def models(self, thread):
        child = await self.open(thread)
        return tuple(model.display_name for model in (await child.request(GetAvailableModels())).models)

    async def configure(self, thread, model, thinking):
        child = await self.open(thread)
        if model is not None:
            await child.request(SetModel.from_name(model))
        if thinking is not None:
            await child.request(SetThinkingLevel(level=thinking))
        state = await child.request(GetState())
        reported = state.model.require_selection(model if model is not None else state.model.display_name)
        return reported.display_name, state.thinking_level.wire_value()
