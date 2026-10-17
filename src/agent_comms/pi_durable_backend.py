"""Opt-in durable runtime. Pi records never escape this backend or its host.

The host is paused at open. Recovery decisions are committed in its own store
before abort marks, and every interrupted item must be disposed before resume.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
from contextlib import asynccontextmanager, contextmanager
from abc import abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Literal
from uuid import uuid4

from pydantic import JsonValue

from .agent_events import Chunk, Thinking, ToolStart, Error

from .agent_backend import (
    AgentBackend, Compacts, CompactionOutcome, ConfiguresModel, ContextUsage, ReportsContext,
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
class DurableEntryQuery:
    min_entry_id: int | None = field(default=None, metadata={'wire_name': 'minEntryId', 'wire_omit_default': True})
    max_entry_id: int | None = field(default=None, metadata={'wire_name': 'maxEntryId', 'wire_omit_default': True})
    order: Literal['ascending', 'descending'] = 'descending'


@dataclass(frozen=True)
class DurableHistoryPage:
    items: tuple[DurableEntry, ...]
    cursor: dict[str, JsonValue] | None = None


class DurableTranscriptRead(TranscriptSource):
    def __init__(self, loop, host):
        self.loop, self.host = loop, host
        page = self.page(limit=1) if host is not None else DurableHistoryPage(())
        self.through = max((item.order for item in page.items), default=0)

    def cut(self):
        return ExternalCut(self.through)

    def page(self, *, limit=128, cursor=None, query=DurableEntryQuery()):
        return self.loop.run(self.host.request(EntriesHostCommand(limit, cursor, query)))

    def records(self, minimum, maximum, order):
        if self.host is None or maximum < minimum:
            return
        cursor = None
        while True:
            page = self.page(cursor=cursor,
                query=DurableEntryQuery(minimum, min(maximum, self.through), order))
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


@dataclass(frozen=True)
class DurableModel:
    provider: str
    model_id: str = field(metadata={'wire_name': 'modelId'})

    @classmethod
    def selected(cls, name):
        return cls(*name.split('/', 1)) if name is not None else None

    @property
    def name(self):
        return f'{self.provider}/{self.model_id}'


@dataclass(frozen=True)
class DurableSelectionEdits:
    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()


@dataclass(frozen=True)
class DurableAgent:
    model: DurableModel | None = field(default=None, metadata={'wire_omit_default': True})
    thinking_level: str | None = field(default=None, metadata={'wire_name': 'thinkingLevel', 'wire_omit_default': True})
    instructions: str | None = field(default=None, metadata={'wire_omit_default': True})
    cwd: str | None = field(default=None, metadata={'wire_omit_default': True})
    extensions: tuple[str, ...] | DurableSelectionEdits | None = field(default=None, metadata={'wire_omit_default': True})
    tools: tuple[str, ...] | DurableSelectionEdits | None = field(default=None, metadata={'wire_omit_default': True})
    model_tools: tuple[str, ...] | DurableSelectionEdits | None = field(default=None, metadata={'wire_name': 'modelTools', 'wire_omit_default': True})


@dataclass(frozen=True)
class DurableConfiguration:
    models: tuple[str, ...]
    model: DurableModel | None = None
    thinking: str | None = None

    def selected(self):
        if self.model is None:
            raise ValueError('Durable session has no configured model')
        return self.model.name, self.thinking


@dataclass(frozen=True)
class InterruptedDurableWork:
    id: str
    kind: str


@dataclass(frozen=True)
class DurableSettlement:
    id: str
    disposition: Literal["ABANDON"]


@dataclass(frozen=True)
class DurableSubmission:
    id: int


@dataclass(frozen=True)
class DurableChange(DeclaredFamily, affix='DurableChange'):
    family_discriminator = 'type'

    def convert(self):
        return ()


@dataclass(frozen=True)
class IndexedDurableChange(DurableChange):
    declared_name = None
    content_index: int = field(metadata={'wire_name': 'contentIndex'})


@dataclass(frozen=True)
class DeltaDurableChange(IndexedDurableChange):
    declared_name = None
    delta: str
    event_type: ClassVar

    def convert(self):
        return (self.event_type(self.delta),)


class TextDeltaDurableChange(DeltaDurableChange):
    event_type = Chunk


class ThinkingDeltaDurableChange(DeltaDurableChange):
    event_type = Thinking


@dataclass(frozen=True)
class BlockDurableChange(IndexedDurableChange):
    block: JsonValue


class TextStartDurableChange(BlockDurableChange): pass
class ThinkingStartDurableChange(BlockDurableChange): pass
class ToolcallStartDurableChange(BlockDurableChange): pass


@dataclass(frozen=True)
class ToolcallDeltaDurableChange(IndexedDurableChange):
    path: tuple[str | int, ...]
    delta: str


@dataclass(frozen=True)
class MessageDurableChange(DurableChange):
    message: JsonValue


@dataclass(frozen=True)
class DurableEvent(DeclaredFamily, affix='DurableEvent'):
    """Decode the pinned SDK event vocabulary once at the host boundary.

    SDK-defined JSON data (tool arguments, stored messages and extension data)
    stays opaque. No consumer recovers protocol fields from those values.
    """
    family_discriminator = 'type'
    unknown_kind = False

    @classmethod
    def wire_kind(cls):
        return cls.declared_name

    def convert(self):
        return ()


@dataclass(frozen=True)
class SnapshotDurableEvent(DurableEvent):
    entries: tuple[JsonValue, ...]
    tools: tuple[JsonValue, ...]
    nested_tools: tuple[JsonValue, ...] = field(metadata={'wire_name': 'nestedTools'})
    compactions: tuple[JsonValue, ...]
    inbox: tuple[JsonValue, ...]
    agent: DurableAgent
    usage: JsonValue
    run: JsonValue = None
    generation: JsonValue = None
    compaction_failure: JsonValue = field(default=None, metadata={'wire_name': 'compactionFailure'})


@dataclass(frozen=True)
class RunDurableEvent(DurableEvent):
    declared_name = None
    inputs: tuple[int, ...]


class RunStartDurableEvent(RunDurableEvent): pass
class RunEndDurableEvent(RunDurableEvent): pass
class TurnStartDurableEvent(DurableEvent): pass
class TurnEndDurableEvent(DurableEvent): pass


@dataclass(frozen=True)
class MessageStartDurableEvent(DurableEvent):
    message: JsonValue


@dataclass(frozen=True)
class MessageUpdateDurableEvent(DurableEvent):
    usage: JsonValue
    changes: tuple[DurableChange, ...]

    def convert(self):
        return tuple(event for change in self.changes for event in change.convert())


@dataclass(frozen=True)
class MessageEndDurableEvent(DurableEvent):
    entry: JsonValue


@dataclass(frozen=True, kw_only=True)
class ToolCallDurableEvent(DurableEvent):
    declared_name = None
    tool_call_id: str = field(metadata={'wire_name': 'toolCallId'})
    tool_name: str = field(metadata={'wire_name': 'toolName'})
    task_id: int | None = field(default=None, metadata={'wire_name': 'taskId'})
    parent_tool_call_id: str | None = field(default=None, metadata={'wire_name': 'parentToolCallId'})
    parent_task_id: int | None = field(default=None, metadata={'wire_name': 'parentTaskId'})


@dataclass(frozen=True)
class ToolExecutionStartDurableEvent(ToolCallDurableEvent):
    args: dict[str, JsonValue]

    def convert(self):
        return (ToolStart(id=self.tool_call_id, name=self.tool_name, args=self.args),)


@dataclass(frozen=True)
class ToolExecutionUpdateDurableEvent(ToolCallDurableEvent):
    output: JsonValue = None
    details: JsonValue = None
    diagnostics: tuple[JsonValue, ...] = ()


@dataclass(frozen=True)
class ToolExecutionEndDurableEvent(ToolCallDurableEvent):
    entry: JsonValue = None
    result: JsonValue = None


@dataclass(frozen=True)
class InboxUpdateDurableEvent(DurableEvent):
    items: tuple[JsonValue, ...]


@dataclass(frozen=True)
class SubmissionDurableEvent(DurableEvent):
    record: JsonValue


@dataclass(frozen=True)
class AutoRetryStartDurableEvent(DurableEvent):
    attempt: int
    at: float
    error_message: str = field(metadata={'wire_name': 'errorMessage'})


@dataclass(frozen=True)
class AutoRetryEndDurableEvent(DurableEvent):
    attempt: int


@dataclass(frozen=True)
class DeferredPollDurableEvent(DurableEvent):
    poll_at: float = field(metadata={'wire_name': 'pollAt'})


@dataclass(frozen=True)
class EntryAppendedDurableEvent(DurableEvent):
    entry: JsonValue


@dataclass(frozen=True)
class AgentChangedDurableEvent(DurableEvent):
    agent: DurableAgent


@dataclass(frozen=True)
class UsageChangedDurableEvent(DurableEvent):
    usage: JsonValue


@dataclass(frozen=True)
class TaskFailedDurableEvent(DurableEvent):
    task_id: int = field(metadata={'wire_name': 'taskId'})
    kind: str
    message: str

    def convert(self):
        return (Error(self.message),)


@dataclass(frozen=True)
class CompactionStartDurableEvent(DurableEvent):
    task_id: int = field(metadata={'wire_name': 'taskId'})
    reason: str
    blocking: bool


@dataclass(frozen=True)
class CompactionEndDurableEvent(DurableEvent):
    task_id: int = field(metadata={'wire_name': 'taskId'})
    reason: str
    error: str | None = None


@dataclass(frozen=True)
class OpenedDurable:
    interrupted: tuple[InterruptedDurableWork, ...]


class HostMessage(DeclaredFamily, affix='HostMessage'):
    @abstractmethod
    def deliver(self, host): ...


@dataclass(frozen=True)
class EventHostMessage(HostMessage):
    event: DurableEvent

    def deliver(self, host):
        host.deliver_event(self.event)


@dataclass(frozen=True)
class ReplyHostMessage(HostMessage):
    id: int

    @abstractmethod
    def complete(self, pending): ...

    def deliver(self, host):
        self.complete(host.pending[self.id])
        del host.pending[self.id]


class ResultHostMessage(ReplyHostMessage):
    declared_name = None

    def complete(self, pending):
        if not isinstance(self, pending.operation.reply_type):
            raise ValueError(f'Wrong reply to {pending.operation.declared_name}: {self.declared_name}')
        pending.succeed(self.result)


@dataclass(frozen=True)
class ErrorHostMessage(ReplyHostMessage):
    error: str

    def complete(self, pending):
        pending.fail(RuntimeError(self.error))


@dataclass(frozen=True)
class FaultHostMessage(HostMessage):
    error: str

    def deliver(self, host):
        raise RuntimeError(self.error)


@dataclass(frozen=True)
class AckHostMessage(ResultHostMessage):
    result: None


@dataclass(frozen=True)
class OpenedHostMessage(ResultHostMessage):
    result: OpenedDurable


@dataclass(frozen=True)
class HistoryHostMessage(ResultHostMessage):
    result: DurableHistoryPage


@dataclass(frozen=True)
class ContextHostMessage(ResultHostMessage):
    result: ContextUsage


@dataclass(frozen=True)
class ConfigurationHostMessage(ResultHostMessage):
    result: DurableConfiguration


@dataclass(frozen=True)
class InspectionHostMessage(ResultHostMessage):
    result: tuple[InterruptedDurableWork, ...]


@dataclass(frozen=True)
class SettlementHostMessage(ResultHostMessage):
    result: DurableSettlement


@dataclass(frozen=True)
class SubmissionHostMessage(ResultHostMessage):
    result: DurableSubmission


@dataclass(frozen=True)
class CompactionHostMessage(ResultHostMessage):
    result: CompactionOutcome


@dataclass(frozen=True)
class HostCommand(DeclaredFamily, affix='HostCommand'):
    family_discriminator = 'command'
    reply_type: ClassVar = AckHostMessage


@dataclass(frozen=True)
class OpenHostCommand(HostCommand):
    path: str
    cwd: str
    agent: DurableAgent
    auth_path: str = field(metadata={'wire_name': 'authPath'})
    reply_type = OpenedHostMessage


@dataclass(frozen=True)
class OpenHistoryHostCommand(HostCommand):
    path: str


class WatchHostCommand(HostCommand): pass
class UnwatchHostCommand(HostCommand): pass
class ResumeHostCommand(HostCommand): pass
class AbortHostCommand(HostCommand): pass
class CloseHostCommand(HostCommand): pass


class InspectHostCommand(HostCommand):
    reply_type = InspectionHostMessage


class ContextUsageHostCommand(HostCommand):
    reply_type = ContextHostMessage


@dataclass(frozen=True)
class SettleHostCommand(HostCommand):
    work: str
    disposition: Literal["ABANDON"]
    reply_type = SettlementHostMessage


@dataclass(frozen=True)
class EntriesHostCommand(HostCommand):
    limit: int
    cursor: dict[str, JsonValue] | None = None
    query: DurableEntryQuery = DurableEntryQuery()
    reply_type = HistoryHostMessage


@dataclass(frozen=True)
class ConfigureHostCommand(HostCommand):
    agent: DurableAgent | None = field(default=None, metadata={'wire_omit_default': True})
    reply_type = ConfigurationHostMessage


@dataclass(frozen=True)
class CompactHostCommand(HostCommand):
    instructions: str | None
    reply_type = CompactionHostMessage


@dataclass(frozen=True)
class SubmitHostCommand(HostCommand):
    content: JsonValue
    request_id: str = field(metadata={'wire_name': 'requestId'})
    when_busy: str = field(metadata={'wire_name': 'whenBusy'})
    reply_type = SubmissionHostMessage


@dataclass(frozen=True)
class HostRequest:
    id: int
    operation: HostCommand


@dataclass(frozen=True)
class PendingHostRequest:
    operation: HostCommand
    future: asyncio.Future

    def succeed(self, result):
        if not self.future.done():
            self.future.set_result(result)

    def fail(self, error):
        if not self.future.done():
            self.future.set_exception(error)


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
            await self.request(OpenHostCommand(str(path), thread.worktree,
                DurableAgent(DurableModel.selected(thread.model), thread.thinking_level),
                str(Path.home() / '.pi/agent/auth.json')))
        except BaseException:
            await self.close()
            raise

    @classmethod
    @asynccontextmanager
    async def history(cls, location):
        host = cls()
        try:
            await host.start(str(Path(location.path).parent))
            await host.request(OpenHistoryHostCommand(location.path))
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
                FieldCodec.decode(HostMessage, json.loads(line)).deliver(self)
            raise EOFError(f'Durable host closed its protocol stream: {await self.stderr}')
        except BaseException as error:
            for pending in self.pending.values():
                pending.fail(error)
            self.pending.clear()
            raise

    @asynccontextmanager
    async def open_turn(self):
        if self.deliver_event is not _event_without_turn:
            raise RuntimeError('A durable turn is already open')
        turn = DurableTurn(self)
        self.deliver_event = turn.receive
        try:
            await self.request(WatchHostCommand())
            yield turn
        finally:
            try:
                # The stop reply follows every event of this watch on stdout.
                # Keep its turn attached until the reader has passed that reply.
                await self.request(UnwatchHostCommand())
            finally:
                self.deliver_event = _event_without_turn

    async def request(self, command: HostCommand):
        if self.reader.done():
            self.reader.result()
        self.sequence += 1
        identity = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[identity] = PendingHostRequest(command, future)
        try:
            request = FieldCodec.encode(HostRequest(identity, command))
            await self.child.write((json.dumps(request) + '\n').encode())
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
        return await self.host.request(InspectHostCommand())

    async def settle(self, thread, work, disposition):
        await self.open(thread)
        await self.host.request(SettleHostCommand(work, disposition))

    async def resume(self, thread):
        await self.open(thread)
        await self.host.request(ResumeHostCommand())

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
        return await self.host.request(ContextUsageHostCommand())

    async def compact(self, thread, instructions):
        await self.open(thread)
        return await self.host.request(CompactHostCommand(instructions))

    async def models(self, thread):
        await self.open(thread)
        return (await self.host.request(ConfigureHostCommand())).models

    async def configure(self, thread, model, thinking):
        await self.open(thread)
        value = await self.host.request(ConfigureHostCommand(
            DurableAgent(DurableModel.selected(model), thinking)))
        return value.selected()
