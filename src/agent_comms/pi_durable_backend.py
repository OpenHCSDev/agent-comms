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

from .agent_backend import AgentBackend, Compacts, CompactionOutcome, ConfiguresModel, ContextUsage, ReportsContext, RuntimeLocation, Steers
from .agent_events import Error
from .native_custody import PiChild
from .external_transcript import ExternalCut
from .native_entries import StoredEntry
from .native_session_files import NativeSessionFiles
from .native_transcript import StoredRecord
from .pi_commands import Compact, PiCommand
from .pi_events import AutoRetryEnd, DurablePiEvent, MessageEnd, RetryEnd
from .pi_payloads import PiPayload, PiResponseData
from .pi_rpc import PiRpcChannel
from .selected_session import SelectedSession
from .transcript_receipts import TranscriptSource
from .unstarted_runtime import UnstartedRuntime


@dataclass(frozen=True)
class DurableDatabase(RuntimeLocation):
    path: str

    def __post_init__(self):
        if not Path(self.path).is_absolute():
            raise ValueError('Durable database path must be absolute')

    def require_forkable(self):
        raise ValueError('Durable database forking is not implemented')

    def select_session(self, directory, execution):
        return DurableSelectedSession(directory, self)

    def history_source(self, root, incarnation):
        return DurableTranscript(self)

    @property
    def read_key(self):
        return self.path

    def storage_files(self):
        return (DurableDatabaseFiles(Path(self.path)),)


@dataclass(frozen=True)
class DurableSelectedSession(SelectedSession):
    location: DurableDatabase

    @property
    def path(self):
        return Path(self.location.path)

    @property
    def session_file(self):
        return self.location.path

    def attest(self, identity):
        raise ValueError('A durable database cannot attest a native Pi session')


class DurableDatabaseFiles(NativeSessionFiles):
    @property
    def members(self):
        return (self.session, *(Path(str(self.session) + suffix) for suffix in ('-wal', '-shm', '-journal', '.lock')))

    @property
    def schema_paths(self):
        return (self.session,)


@dataclass(frozen=True)
class DurableEntryQuery:
    min_entry_id: int | None = field(default=None, metadata={'wire_name': 'minEntryId', 'wire_omit_default': True})
    max_entry_id: int | None = field(default=None, metadata={'wire_name': 'maxEntryId', 'wire_omit_default': True})
    order: str = 'descending'


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

    def page(self, *, limit=128, cursor=None, query=DurableEntryQuery()):
        return self.execute(self, Entries(limit, cursor, query))

    def cut(self):
        return ExternalCut(max((entry.id for entry in self.page(limit=1).items), default=0))

    def records(self, minimum, maximum, order):
        if maximum < minimum:
            return
        cursor = None
        while True:
            page = self.page(cursor=cursor, query=DurableEntryQuery(minimum, maximum, order))
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
class ModelRef(PiPayload):
    provider: str
    model_id: str = field(metadata={'wire_name': 'modelId'})


@dataclass(frozen=True)
class AgentSettings(PiPayload):
    model: ModelRef | None = field(default=None, metadata={'wire_omit_default': True})
    thinking_level: str | None = field(default=None, metadata={'wire_name': 'thinkingLevel', 'wire_omit_default': True})
    instructions: str | None = field(default=None, metadata={'wire_omit_default': True})

    @classmethod
    def selected(cls, model, thinking):
        return cls(ModelRef(*model.split('/', 1)) if model else None, thinking)


@dataclass(frozen=True)
class ConfigurationData(PiResponseData):
    models: tuple[str, ...]
    model: ModelRef | None = None
    thinking: str | None = None

    def selected(self):
        if self.model is None:
            raise ValueError('Durable session has no configured model')
        return f'{self.model.provider}/{self.model.model_id}', self.thinking


@dataclass(frozen=True)
class InterruptedWork:
    id: str
    kind: str


@dataclass(frozen=True)
class RecoveryData(PiResponseData):
    work: tuple[InterruptedWork, ...]


@dataclass(frozen=True)
class ContextUsageData(ContextUsage, PiResponseData):
    pass


@dataclass(frozen=True)
class OutcomeData(PiResponseData):
    outcome: CompactionOutcome


class DurableRetryEnd(RetryEnd, DurablePiEvent):
    wire_kind = AutoRetryEnd.wire_kind


@dataclass(frozen=True, kw_only=True)
class StoredMessageEnd(MessageEnd, DurablePiEvent):
    entry: StoredEntry
    wire_kind = MessageEnd.wire_kind


class Snapshot(DurablePiEvent):
    pass


@dataclass(frozen=True)
class AgentChanged(DurablePiEvent):
    agent: AgentSettings


@dataclass(frozen=True)
class TaskFailed(DurablePiEvent):
    task_id: int = field(metadata={'wire_name': 'taskId'})
    message: str

    def convert(self):
        return (Error(self.message),)


class RunStart(DurablePiEvent): pass
class RunEnd(DurablePiEvent): pass
class InboxUpdate(DurablePiEvent): pass
class Submission(DurablePiEvent): pass
class DeferredPoll(DurablePiEvent): pass
class UsageChanged(DurablePiEvent): pass


class HostQuery(PiCommand):
    declared_name = None
    strict_response = True


@dataclass(frozen=True)
class Open(HostQuery):
    path: str
    cwd: str
    agent: AgentSettings
    auth_path: str = field(metadata={'wire_name': 'authPath'})
    response_payload = RecoveryData


@dataclass(frozen=True)
class OpenHistory(HostQuery):
    path: str


class Watch(HostQuery): pass
class Unwatch(HostQuery): pass
class Resume(HostQuery): pass


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
    query: DurableEntryQuery = DurableEntryQuery()
    response_payload = HistoryData


@dataclass(frozen=True)
class Configure(HostQuery):
    agent: AgentSettings | None = field(default=None, metadata={'wire_omit_default': True})
    response_payload = ConfigurationData


class ContextUsageQuery(HostQuery):
    response_payload = ContextUsageData


class CompactDurable(Compact):
    strict_response = True
    response_payload = OutcomeData


class PiDurableBackend(AgentBackend, Steers, Compacts, ReportsContext, ConfiguresModel):
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
        if self.child is None:
            path = Path(thread.runtime.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with ExitStack() as resources:
                lock = resources.enter_context(path.with_suffix(path.suffix + '.lock').open('a+b'))
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.child = await PiChild.start_command(('node', str(Path(__file__).with_name('pi_durable_host.mjs'))),
                    cwd=thread.worktree, pass_fds=(lock.fileno(),), event_type=DurablePiEvent, resources=resources.pop_all())
            await self.child.request(Open(str(path), thread.worktree, AgentSettings.selected(thread.model, thread.thinking_level), str(Path.home() / '.pi/agent/auth.json')))
        return self.child

    async def interrupted(self, thread):
        child = await self.open(thread)
        return (await child.request(Inspect())).work

    async def settle(self, thread, work, disposition):
        child = await self.open(thread)
        await child.request(Settle(work, disposition))

    async def resume(self, thread):
        child = await self.open(thread)
        await child.request(Resume())

    async def run_turn(self, thread, content, delivery):
        async with self.lock:
            child = await self.open(thread)
            async with child.reader.turn() as turn:
                await child.request(Watch())
                try:
                    await self.submit_turn(content, delivery)
                    async for event in turn.converted():
                        yield event
                finally:
                    await child.request(Unwatch())

    async def submit_turn(self, content, delivery):
        raise RuntimeError('Durable worker integration is unfinished: Core InputId and send-disposition integration required')

    async def abort(self, owner):
        if self.child is not None:
            await self.child.shutdown()

    async def close_idle(self):
        async with self.lock:
            if self.child is not None:
                await self.child.shutdown()
                self.child = None

    async def context_usage(self, thread):
        child = await self.open(thread)
        return await child.request(ContextUsageQuery())

    async def compact(self, thread, instructions):
        child = await self.open(thread)
        return (await child.request(CompactDurable(custom_instructions=instructions))).outcome

    async def models(self, thread):
        child = await self.open(thread)
        return (await child.request(Configure())).models

    async def configure(self, thread, model, thinking):
        child = await self.open(thread)
        return (await child.request(Configure(AgentSettings.selected(model, thinking)))).selected()
