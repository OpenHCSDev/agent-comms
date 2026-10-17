"""Opt-in durable runtime. Pi records never escape this backend or its host.

The host is paused at open. Recovery decisions are committed in its own store
before abort marks, and every interrupted item must be disposed before resume.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

from .agent_events import Chunk, Thinking, ToolStart, Error

from .agent_backend import (
    AgentBackend, Compacts, ConfiguresModel, ContextUsage,
    FailedCompaction, NothingToCompaction, PlacedCompaction, StaleCompaction, ReportsContext,
    RuntimeLocation, Steers,
)
from .child_process import AttachedChild
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
from .external_transcript import ExternalRecord, ExternalCut
from .transcript_receipts import TranscriptSource
from .transcript_events import UserTranscript, AssistantTranscript, NoticeTranscript, TurnDigest
from .unstarted_runtime import UnstartedRuntime
from .native_session_files import NativeSessionFiles
from .selected_session import SelectedSession


@dataclass(frozen=True)
class DurableDatabase(RuntimeLocation):
    """A private SQLite database bound by the owning thread's backend."""
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
        return (self.session, *(Path(str(self.session) + suffix)
                for suffix in ('-wal', '-shm', '-journal', '.lock')))

    @property
    def schema_paths(self):
        return (self.session,)


@dataclass(frozen=True)
class DurableRecord(ExternalRecord):
    @property
    def turn_events(self):
        return self.events

    def completed_turn(self, records):
        return ()


class DurableInputRecord(DurableRecord):
    def completed_turn(self, records):
        return (TurnDigest.from_records(str(self.seq), records),)


@dataclass(frozen=True)
class DurableEntry(DeclaredFamily, affix='DurableEntry'):
    order: int
    text: str
    transcript_type: ClassVar = NoticeTranscript
    record_type: ClassVar = DurableRecord

    def record(self):
        return self.record_type(self.order, (self.transcript_type(self.text),))


class UserDurableEntry(DurableEntry):
    transcript_type = UserTranscript
    record_type = DurableInputRecord


class AssistantDurableEntry(DurableEntry):
    transcript_type = AssistantTranscript


class SystemDurableEntry(DurableEntry): pass
class ToolResultDurableEntry(DurableEntry): pass
class CompactionDurableEntry(DurableEntry): pass
class ResetDurableEntry(DurableEntry): pass


@dataclass(frozen=True)
class DurableHistoryPage:
    items: tuple[DurableEntry, ...]
    cursor: str | None = None

    @classmethod
    def read(cls, loop, host, *, limit=128, cursor=None, query=None):
        return FieldCodec.decode(cls, loop.run(host.request(
            'entries', limit=limit, cursor=cursor, query=query or {})))


class DurableTranscriptRead(TranscriptSource):
    def __init__(self, loop, host):
        self.loop, self.host = loop, host
        page = DurableHistoryPage.read(loop, host, limit=1) if host is not None else DurableHistoryPage(())
        self.through = max((item.order for item in page.items), default=0)

    def cut(self):
        return ExternalCut(self.through)

    def records(self, minimum, maximum, order):
        if self.host is None or maximum < minimum:
            return
        cursor = None
        while True:
            page = DurableHistoryPage.read(self.loop, self.host, cursor=cursor,
                query={'minEntryId': minimum, 'maxEntryId': min(maximum, self.through), 'order': order})
            for item in page.items:
                yield item.record()
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


class DurableEvent(DeclaredFamily, affix='DurableEvent'):
    """One converter family for the pinned 1.3.0 watchEvents vocabulary."""
    unknown_kind = False

    def __init__(self, payload):
        self.payload = payload

    @classmethod
    def wire_kind(cls):
        return cls.declared_name

    def convert(self):
        return ()


class SnapshotDurableEvent(DurableEvent): pass
class RunStartDurableEvent(DurableEvent): pass
class RunEndDurableEvent(DurableEvent): pass
class TurnStartDurableEvent(DurableEvent): pass
class TurnEndDurableEvent(DurableEvent): pass
class MessageStartDurableEvent(DurableEvent): pass
class MessageUpdateDurableEvent(DurableEvent):
    def convert(self):
        result = []
        for change in self.payload['changes']:
            if change['type'] == 'text_delta':
                result.append(Chunk(change['delta']))
            elif change['type'] == 'thinking_delta':
                result.append(Thinking(change['delta']))
        return tuple(result)
class MessageEndDurableEvent(DurableEvent): pass
class ToolExecutionStartDurableEvent(DurableEvent):
    def convert(self):
        return (ToolStart(id=self.payload['toolCallId'], name=self.payload['toolName'], args=self.payload['args']),)
class ToolExecutionUpdateDurableEvent(DurableEvent): pass
class ToolExecutionEndDurableEvent(DurableEvent): pass
class InboxUpdateDurableEvent(DurableEvent): pass
class SubmissionDurableEvent(DurableEvent): pass
class AutoRetryStartDurableEvent(DurableEvent): pass
class AutoRetryEndDurableEvent(DurableEvent): pass
class DeferredPollDurableEvent(DurableEvent): pass
class EntryAppendedDurableEvent(DurableEvent): pass
class AgentChangedDurableEvent(DurableEvent): pass
class UsageChangedDurableEvent(DurableEvent): pass
class TaskFailedDurableEvent(DurableEvent):
    def convert(self):
        return (Error(self.payload['message']),)
class CompactionStartDurableEvent(DurableEvent): pass
class CompactionEndDurableEvent(DurableEvent): pass


def _event_without_turn(event):
    raise RuntimeError(f'Durable event {event.wire_kind()!r} arrived with no open turn')


class DurableTurn:
    """The open turn owns every committed event until its watch is drained."""
    def __init__(self, host):
        self.host = host
        self.events = asyncio.Queue(maxsize=100)

    def receive(self, event):
        # Never block the protocol reader behind a slow ACP consumer: abort and
        # other replies use that same pipe. Overflow is a failure, not lost paint.
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
            event = await self.next_event()
            for converted in event.convert():
                yield converted


class DurableHost:
    """Own the child, its JSONL reader and its exclusive database lock together."""
    def __init__(self):
        self.child = None
        self.lock_file = None
        self.reader = None
        self.stderr = None
        self.pending = {}
        self.deliver_event = _event_without_turn
        self.sequence = 0

    async def open(self, location, thread):
        if self.child is not None:
            return
        path = Path(location.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_file = path.with_suffix(path.suffix + '.lock').open('a+b')
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            await self.start(thread.worktree, (self.lock_file.fileno(),))
            agent = {}
            if thread.model:
                provider, model = thread.model.split('/', 1)
                agent['model'] = {'provider': provider, 'modelId': model}
            if thread.thinking_level:
                agent['thinkingLevel'] = thread.thinking_level
            await self.request('open', path=str(path), cwd=thread.worktree, agent=agent,
                               authPath=str(Path.home() / '.pi/agent/auth.json'))
        except BaseException:
            await self.close()
            raise

    @classmethod
    @asynccontextmanager
    async def history(cls, location):
        host = cls()
        try:
            await host.start(str(Path(location.path).parent))
            await host.request('open_history', path=location.path)
            yield host
        finally:
            await host.close()

    async def start(self, cwd, pass_fds=()):
        self.child = await AttachedChild.start(
            ('node', str(Path(__file__).with_name('pi_durable_host.mjs'))),
            cwd=cwd, pass_fds=pass_fds,
        )
        self.reader = asyncio.create_task(self.read())
        self.stderr = asyncio.create_task(self.child.stderr_tail())

    async def read(self):
        try:
            while line := await self.child.stdout.readline():
                message = json.loads(line)
                if 'event' in message:
                    payload = message['event']
                    event = DurableEvent.decode(payload['type'])(payload)
                    self.deliver_event(event)
                else:
                    future = self.pending.pop(message['id'])
                    if not future.done():
                        if 'error' in message:
                            future.set_exception(RuntimeError(message['error']))
                        else:
                            future.set_result(message['result'])
            raise EOFError(f'Durable host closed its protocol stream: {await self.stderr}')
        except BaseException as error:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(error)
            self.pending.clear()
            raise

    @asynccontextmanager
    async def open_turn(self):
        if self.deliver_event is not _event_without_turn:
            raise RuntimeError('A durable turn is already open')
        turn = DurableTurn(self)
        self.deliver_event = turn.receive
        try:
            await self.request('watch')
            yield turn
        finally:
            try:
                # The stop reply follows every event of this watch on stdout.
                # Keep its turn attached until the reader has passed that reply.
                await self.request('unwatch')
            finally:
                self.deliver_event = _event_without_turn

    async def request(self, command, **payload):
        if self.reader.done():
            self.reader.result()
        self.sequence += 1
        identity = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[identity] = future
        try:
            await self.child.write((json.dumps(dict(id=identity, command=command, **payload)) + '\n').encode())
            return await future
        except BaseException:
            # A cancelled wait never erases a possibly submitted host request.
            # The reader owns its reply until it arrives or this child closes.
            if not future.done():
                future.cancel()
            raise

    async def close(self):
        try:
            if self.child is not None:
                await self.child.stop()
            for task in (self.reader, self.stderr):
                if task is not None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        finally:
            self.child = self.reader = self.stderr = None
            if self.lock_file is not None:
                self.lock_file.close()
                self.lock_file = None


class PiDurableBackend(AgentBackend, Steers, Compacts, ReportsContext, ConfiguresModel):
    """Explicitly selected only; PiNativeBackend remains the new-thread default.

    This draft checkpoint intentionally refuses turns until Core's input
    owner supplies the initial InputId and generic send disposition. No provider
    call can slip through an unfinished worker integration.
    """
    location_type = DurableDatabase
    unstarted_type = UnstartedRuntime
    event_kinds = frozenset(member.wire_kind() for member in DurableEvent.members_with(DurableEvent))

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
        return tuple(await self.host.request('inspect'))

    async def settle(self, thread, work, disposition):
        await self.open(thread)
        await self.host.request('settle', work=work, disposition=disposition)

    async def resume(self, thread):
        await self.open(thread)
        await self.host.request('resume')

    async def run_turn(self, thread, content, delivery):
        async with self.lock:
            await self.open(thread)
            async with self.host.open_turn() as turn:
                await self.submit_turn(content, delivery)
                async for event in turn.converted():
                    yield event

    async def submit_turn(self, content, delivery):
        # Input ownership must be connected before provider submission is enabled.
        raise RuntimeError('Durable worker integration is unfinished: initial Core InputId and send-disposition integration required')

    async def abort(self, owner):
        # Closing never resumes interrupted work or replays an uncertain input.
        await self.host.close()

    async def close_idle(self):
        async with self.lock:
            await self.host.close()

    async def context_usage(self, thread):
        await self.open(thread)
        value = await self.host.request('context_usage')
        return ContextUsage(value['tokens'], value['window'])

    async def compact(self, thread, instructions):
        await self.open(thread)
        result = await self.host.request('compact', instructions=instructions)
        if result['status'] == 'nothing':
            return NothingToCompaction()
        if result['status'] == 'placed':
            return PlacedCompaction(result['summary'], result['first_kept'], result['tokens_before'])
        if result['status'] == 'stale':
            return StaleCompaction()
        return FailedCompaction(result['message'])

    async def models(self, thread):
        await self.open(thread)
        return tuple((await self.host.request('configure'))['models'])

    async def configure(self, thread, model, thinking):
        await self.open(thread)
        agent = {}
        if model is not None:
            provider, model_id = model.split('/', 1)
            agent['model'] = {'provider': provider, 'modelId': model_id}
        if thinking is not None:
            agent['thinkingLevel'] = thinking
        value = await self.host.request('configure', agent=agent)
        selected = value['model']
        return f"{selected['provider']}/{selected['modelId']}", value['thinking']
