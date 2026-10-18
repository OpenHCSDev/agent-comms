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

from .agent_backend import AgentBackend, RuntimeLocation, Steers
from .agent_events import Error, Done, OriginalInputStarted, FollowingInputStarted, InputStarted
from .image_inputs import ImageInput
from .maintenance_barrier import MaintenanceBarrier
from .pi_native_backend import PiCapabilities
from .native_child import PiChild
from .external_transcript import ExternalCut
from .native_entries import StoredEntry
from .native_session_files import SessionFiles
from .native_transcript import StoredRecord
from .pi_commands import Abort, PiCommand, SetModel, SetThinkingLevel
from .pi_events import AutoRetryEnd, DurablePiEvent, RetryEnd
from .pi_payloads import PiResponseData
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
        path = self.session
        return (path, *(path.with_name(path.name + suffix) for suffix in ('-wal', '-shm', '-journal')), self.writer_lock)

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


class DurableRetryEnd(RetryEnd, DurablePiEvent):
    wire_kind = AutoRetryEnd.wire_kind


@dataclass(frozen=True)
class TaskFailed(DurablePiEvent):
    task_id: int = field(metadata={'wire_name': 'taskId'})
    message: str

    def convert(self):
        return (Error(self.message),)


class HostQuery(PiCommand):
    declared_name = None
    strict_response = True


@dataclass(frozen=True)
class Open(HostQuery):
    path: str
    cwd: str
    provider: str | None
    model_id: str | None = field(metadata={'wire_name': 'modelId'})
    thinking_level: str | None = field(metadata={'wire_name': 'thinkingLevel'})
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


@dataclass(frozen=True)
class SubmissionData(PiResponseData):
    id: int


@dataclass(frozen=True)
class Submit(HostQuery):
    request_id: str = field(metadata={'wire_name': 'requestId'})
    text: str
    images: tuple[ImageInput, ...] = ()
    when_busy: str = field(default='steer', metadata={'wire_name': 'whenBusy'})
    response_payload = SubmissionData


class SubmitInitial(Submit):
    pass


class AwaitTurn(HostQuery):
    pass


@dataclass(frozen=True, kw_only=True)
class DurableOriginalStarted(DurablePiEvent, OriginalInputStarted):
    native_id: str
    text: str

    def convert(self):
        return (self,)


@dataclass(frozen=True, kw_only=True)
class DurableFollowingStarted(DurablePiEvent, FollowingInputStarted):
    native_id: str
    text: str

    def convert(self):
        return (self,)


@dataclass(frozen=True, kw_only=True)
class DurableTurnEnd(DurablePiEvent):
    text: str
    ok: bool
    reason: str | None = None

    def convert(self):
        return (Done(text=self.text, ok=self.ok, reason_code=self.reason),)


class DurableTurn:
    """One Core turn's ingress and its existing child, not another transport."""

    def __init__(self, child, delivery, barrier):
        self.child, self.delivery, self.barrier = child, delivery, barrier

    async def submit(self, command, public_id):
        async with self.barrier.ingress_async() as status:
            with status.phase.send_running(self.delivery.send_boundary, public_id,
                                           command.request_id, command.text) as outcome:
                completion = outcome.dispatch(self, self, command,
                                              lambda: self.child.request(command))
        return await completion

    def refuse(self, turn, input_ids):
        raise RuntimeError('Durable input refused before submission')

    async def place(self, turn, item):
        return await self.submit(Submit(request_id=item.input_id.value,
            text=item.content.text, images=item.content.images,
            when_busy=item.when_busy.value), item.input_id.value)

    async def use_settings(self, turn, item):
        await self.child.request(SetModel.from_name(item.model))
        await self.child.request(SetThinkingLevel(level=item.thinking_level))
        return True

    async def send_now(self, turn, item):
        raise ValueError('Pi Durable does not support send-now')

    async def clear_queue(self, turn, item):
        raise ValueError('Pi Durable queue withdrawal is not implemented')

    async def forward(self):
        while True:
            item = await self.delivery.inbox.get()
            if not await item.deliver_to(self, self):
                return


class PiDurableBackend(PiCapabilities, Steers, AgentBackend):
    location_type = DurableDatabase
    unstarted_type = UnstartedRuntime
    event_kinds = frozenset(kind for member in DurablePiEvent.members_with(DurablePiEvent) for kind in member.known_kinds())

    def __init__(self, root):
        self.child: PiChild | None = None
        self.lock = asyncio.Lock()
        self.owner = None
        self.barrier = MaintenanceBarrier(root / 'registry.json')

    @classmethod
    def for_worker(cls, runner, session_id):
        return cls(runner.comms.root)

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

    async def request(self, thread, command):
        return await (await self.open(thread)).request(command)

    async def run_turn(self, thread, content, delivery):
        child = await self.open(thread)
        turn = DurableTurn(child, delivery, self.barrier)
        self.owner = asyncio.current_task()
        try:
            async with child.failures(), child.reader.turn():
                await child.request(Watch())
                try:
                    await turn.submit(SubmitInitial(request_id=uuid4().hex,
                        text=content.text, images=content.images), None)
                    child.forward_input(turn.forward)
                    completion = asyncio.create_task(child.request(AwaitTurn()))
                    try:
                        async for event in child.reader.converted():
                            if isinstance(event, InputStarted):
                                if not delivery.input_started(event.id, event.native_id, event.text):
                                    await child.request(Abort())
                                    raise RuntimeError('Core refused a started durable input')
                            yield event
                            if isinstance(event, Done):
                                await completion
                                return
                        raise EOFError('Durable host ended before turn settlement')
                    finally:
                        completion.cancel()
                        await asyncio.gather(completion, return_exceptions=True)
                finally:
                    await child.request(Unwatch())
        finally:
            await child.finish_input()
            self.owner = None

    async def abort(self, owner):
        if self.owner is owner and self.child is not None:
            await self.child.request(Abort())
