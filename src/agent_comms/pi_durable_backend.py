"""Opt-in Pi Durable: existing Pi records, RPC correlation and history owners."""
from __future__ import annotations

import asyncio
import fcntl
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from uuid import uuid4

from pydantic import JsonValue

from .agent_backend import AgentBackend, Compacts, CompactionOutcome, ConfiguresModel, ContextUsage, ReportsContext, RuntimeLocation, Steers
from .agent_events import Error
from .child_process import AttachedChild
from .external_transcript import ExternalCut
from .native_entries import StoredEntry
from .native_session_files import NativeSessionFiles
from .native_transcript import StoredRecord
from .pi_commands import Compact, PiCommand
from .pi_events import DurablePiEvent, Response
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


class DurableTranscriptRead(TranscriptSource):
    def __init__(self, loop, host):
        self.loop, self.host = loop, host
        page = self.page(limit=1) if host is not None else HistoryData(())
        self.through = max((entry.id for entry in page.items), default=0)

    def cut(self):
        return ExternalCut(self.through)

    def page(self, *, limit=128, cursor=None, query=DurableEntryQuery()):
        return self.loop.run(self.host.request(Entries(limit, cursor, query)))

    def records(self, minimum, maximum, order):
        if self.host is None or maximum < minimum:
            return
        cursor = None
        while True:
            page = self.page(cursor=cursor, query=DurableEntryQuery(minimum, min(maximum, self.through), order))
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
class DurableTranscript(TranscriptSource):
    location: DurableDatabase

    @contextmanager
    def reading(self):
        if not Path(self.location.path).is_file():
            yield DurableTranscriptRead(None, None)
            return
        with asyncio.Runner() as loop:
            lifetime = DurableHost.history(self.location)
            host = loop.run(lifetime.__aenter__())
            try:
                yield DurableTranscriptRead(loop, host)
            finally:
                loop.run(lifetime.__aexit__(None, None, None))

    def cut(self):
        with self.reading() as reader:
            return reader.cut()


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


class Snapshot(DurablePiEvent):
    pass


@dataclass(frozen=True)
class AgentChanged(DurablePiEvent):
    agent: AgentSettings


@dataclass(frozen=True)
class TaskFailed(DurablePiEvent):
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


def _event_without_turn(event):
    raise RuntimeError(f'Pi event {event.declared_name!r} arrived with no open turn')


class DurableTurn:
    def __init__(self, host):
        self.host = host
        self.events = asyncio.Queue(maxsize=100)

    def receive(self, event):
        self.events.put_nowait(event)

    async def next_event(self):
        waiting = asyncio.create_task(self.events.get())
        try:
            ready, _ = await asyncio.wait((waiting, self.host.reader), return_when=asyncio.FIRST_COMPLETED)
            if self.host.reader in ready:
                self.host.reader.result()
            return waiting.result()
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)

    async def converted(self):
        while True:
            for event in (await self.next_event()).convert():
                yield event


class DurableHost:
    def __init__(self):
        self.child = self.lock_file = self.reader = self.stderr = self.channel = None
        self.deliver_event = _event_without_turn

    async def open(self, location, thread):
        if self.child is not None:
            return
        path = Path(location.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_file = path.with_suffix(path.suffix + '.lock').open('a+b')
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            await self.start(thread.worktree, (self.lock_file.fileno(),))
            await self.request(Open(str(path), thread.worktree, AgentSettings.selected(thread.model, thread.thinking_level), str(Path.home() / '.pi/agent/auth.json')))
        except BaseException:
            await self.close()
            raise

    @classmethod
    @asynccontextmanager
    async def history(cls, location):
        host = cls()
        try:
            await host.start(str(Path(location.path).parent))
            await host.request(OpenHistory(location.path))
            yield host
        finally:
            await host.close()

    async def start(self, cwd, pass_fds=()):
        self.child = await AttachedChild.start(('node', str(Path(__file__).with_name('pi_durable_host.mjs'))), cwd=cwd, pass_fds=pass_fds)
        self.channel = PiRpcChannel(self.child.stdout)
        self.reader = asyncio.create_task(self.read())
        self.stderr = asyncio.create_task(self.child.stderr_tail())

    async def read(self):
        try:
            while (event := await self.channel.receive(strict=True)) is not None:
                if isinstance(event, Response):
                    self.channel.correlate(event)
                else:
                    self.deliver_event(event)
            raise EOFError(f'Durable host closed: {await self.stderr}')
        finally:
            self.channel.pending.cancel_all()

    @asynccontextmanager
    async def open_turn(self):
        if self.deliver_event is not _event_without_turn:
            raise RuntimeError('A durable turn is already open')
        turn = DurableTurn(self)
        self.deliver_event = turn.receive
        try:
            await self.request(Watch())
            yield turn
        finally:
            try:
                await self.request(Unwatch())
            finally:
                self.deliver_event = _event_without_turn

    async def request(self, command):
        self.reader.result() if self.reader.done() else None
        command = replace(command, id=uuid4().hex)
        future = self.channel.track(command)
        try:
            await self.child.write(self.channel.command_bytes(command))
            ready, _ = await asyncio.wait((future, self.reader), return_when=asyncio.FIRST_COMPLETED)
            if self.reader in ready:
                self.reader.result()
            response = future.result()
            if not response.success:
                raise RuntimeError(response.error)
            return response.data.require_payload()
        finally:
            future.cancel()

    async def close(self):
        try:
            if self.child is not None:
                await self.child.stop()
            for task in (self.reader, self.stderr):
                if task is not None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        finally:
            self.child = self.reader = self.stderr = self.channel = None
            if self.lock_file is not None:
                self.lock_file.close()
                self.lock_file = None


class PiDurableBackend(AgentBackend, Steers, Compacts, ReportsContext, ConfiguresModel):
    location_type = DurableDatabase
    unstarted_type = UnstartedRuntime
    event_kinds = frozenset(member.wire_kind() for member in DurablePiEvent.members_with(DurablePiEvent) if not member.unknown_kind)

    def __init__(self):
        self.host = DurableHost()
        self.lock = asyncio.Lock()

    @classmethod
    def for_worker(cls, runner, session_id):
        return cls()

    @classmethod
    def new_location(cls, root, worktree):
        return DurableDatabase(str(Path(root).absolute() / 'durable-sessions' / f'{uuid4().hex}.sqlite3'))

    async def open(self, thread):
        await self.host.open(thread.runtime, thread)

    async def interrupted(self, thread):
        await self.open(thread)
        return (await self.host.request(Inspect())).work

    async def settle(self, thread, work, disposition):
        await self.open(thread)
        await self.host.request(Settle(work, disposition))

    async def resume(self, thread):
        await self.open(thread)
        await self.host.request(Resume())

    async def run_turn(self, thread, content, delivery):
        async with self.lock:
            await self.open(thread)
            async with self.host.open_turn() as turn:
                await self.submit_turn(content, delivery)
                async for event in turn.converted():
                    yield event

    async def submit_turn(self, content, delivery):
        raise RuntimeError('Durable worker integration is unfinished: Core InputId and send-disposition integration required')

    async def abort(self, owner):
        await self.host.close()

    async def close_idle(self):
        async with self.lock:
            await self.host.close()

    async def context_usage(self, thread):
        await self.open(thread)
        return await self.host.request(ContextUsageQuery())

    async def compact(self, thread, instructions):
        await self.open(thread)
        return (await self.host.request(CompactDurable(custom_instructions=instructions))).outcome

    async def models(self, thread):
        await self.open(thread)
        return (await self.host.request(Configure())).models

    async def configure(self, thread, model, thinking):
        await self.open(thread)
        return (await self.host.request(Configure(AgentSettings.selected(model, thinking)))).selected()
