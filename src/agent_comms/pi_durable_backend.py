"""Opt-in durable runtime. Pi records never escape this backend or its host.

The host is paused at open. Recovery decisions are committed in its own store
before abort marks, and every interrupted item must be disposed before resume.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from .agent_backend import (
    AgentBackend, Compacts, ConfiguresModel, ContextUsage, EntryPage, EntryRef,
    FailedCompaction, NothingToCompaction, PlacedCompaction, StaleCompaction, ReadsHistory, ReportsContext,
    RuntimeLocation, Steers, TranscriptKind, TranscriptEntry,
)
from .child_process import AttachedChild
from .declared_family import DeclaredFamily


@dataclass(frozen=True)
class DurableDatabase(RuntimeLocation):
    """A private SQLite database, selected by the thread's runtime envelope."""
    path: str
    available: ClassVar[bool] = True

    def __post_init__(self):
        if not Path(self.path).is_absolute():
            raise ValueError('Durable database path must be absolute')

    @classmethod
    def from_wire(cls, value):
        if not isinstance(value, dict) or set(value) != {'path'} or not isinstance(value['path'], str):
            raise ValueError('Durable location requires exactly an absolute path')
        return cls(value['path'])

    def location_payload(self):
        return {'path': self.path}


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
        from .agent_events import Chunk, Thinking
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
        from .agent_events import ToolStart
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
        from .agent_events import Error
        return (Error(self.payload['message']),)
class CompactionStartDurableEvent(DurableEvent): pass
class CompactionEndDurableEvent(DurableEvent): pass


class DurableHost:
    """Own the child, its JSONL reader and its exclusive database lock together."""
    def __init__(self):
        self.child = None
        self.lock_file = None
        self.reader = None
        self.stderr = None
        self.pending = {}
        self.events = None
        self.sequence = 0

    async def open(self, location, thread):
        if self.child is not None:
            return
        path = Path(location.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_file = path.with_suffix(path.suffix + '.lock').open('a+b')
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.child = await AttachedChild.start(
                ('node', str(Path(__file__).with_name('pi_durable_host.mjs'))), cwd=thread.worktree,
                pass_fds=(self.lock_file.fileno(),),
            )
            self.reader = asyncio.create_task(self.read())
            self.stderr = asyncio.create_task(self.child.discard_stderr())
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

    async def read(self):
        try:
            while line := await self.child.stdout.readline():
                message = json.loads(line)
                if 'event' in message:
                    payload = message['event']
                    event = DurableEvent.decode(payload['type'])(payload)
                    if self.events is not None:
                        await self.events.put(event)
                else:
                    future = self.pending.pop(message['id'])
                    if not future.done():
                        if 'error' in message:
                            future.set_exception(RuntimeError(message['error']))
                        else:
                            future.set_result(message['result'])
            raise EOFError('Durable host closed its protocol stream')
        except BaseException as error:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(error)
            self.pending.clear()
            raise

    async def request(self, command, **payload):
        self.sequence += 1
        identity = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[identity] = future
        try:
            await self.child.write((json.dumps(dict(id=identity, command=command, **payload)) + '\n').encode())
            return await future
        except BaseException:
            # A cancelled wait never erases a possibly admitted host request.
            # The reader owns its receipt until it arrives or this child closes.
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


class PiDurableBackend(AgentBackend, Steers, Compacts, ReportsContext, ReadsHistory, ConfiguresModel):
    """Explicitly selected only; PiNativeBackend remains the new-thread default.

    This draft checkpoint intentionally refuses turns until Core's admission
    owner supplies the initial InputId and generic send disposition. No provider
    call can slip through an unfinished worker integration.
    """
    location_type = DurableDatabase
    event_kinds = frozenset(member.wire_kind() for member in DurableEvent.members_with(DurableEvent))

    def __init__(self):
        self.host = DurableHost()
        self.lock = asyncio.Lock()

    @classmethod
    def for_worker(cls, runner, session_id):
        return cls()

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
        raise RuntimeError('Durable worker admission is unfinished: initial Core InputId and send-disposition integration required')
        yield  # AsyncIterator contract; no input is submitted by this checkpoint.

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

    async def entries(self, thread, before, limit):
        await self.open(thread)
        page = await self.host.request('entries', limit=limit, cursor=before.backend_id if before else None)
        entries = tuple(TranscriptEntry(EntryRef(item['order'], item['id']), TranscriptKind.decode(item['kind']), item['text'], None) for item in page['items'])
        return EntryPage(entries, EntryRef(entries[-1].ref.order, page['cursor']) if page.get('cursor') and entries else None)

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
