"""Opt-in Pi Durable: existing Pi records, RPC correlation and history owners."""
from __future__ import annotations

import asyncio
import fcntl
from collections.abc import Callable, Iterator
from contextlib import AsyncExitStack, ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

from pydantic import JsonValue

from .agent_backend import (
    AgentBackend,
    CompactionOutcome,
    InputId,
    RuntimeLocation,
    Steers,
    TurnInboxItem,
    WhenBusy,
)
from .agent_events import (
    AgentInfo,
    Done,
    Error,
    FollowingInputRefused,
    FollowingInputStarted,
    OriginalInputStarted,
)
from .coordination_errors import IdentityConflict
from .coordinator import Coordination
from .external_transcript import ExternalCut
from .image_inputs import ImageInput
from .launch_runtime import RestartEnvironment
from .maintenance_barrier import MaintenanceBarrier
from .native_child import PiChild
from .native_entries import StoredEntry
from .native_pi import (
    ContextProof,
    DurableContextProof,
    NativeContextRecord,
    NativeTurnResult,
)
from .native_session_files import SessionFiles
from .native_session_reopen import NativeSessionIdentity
from .native_tool_call import ToolTransport
from .native_transcript import StoredRecord
from .pi_capabilities import PiCapabilities
from .pi_commands import (
    Abort,
    Compact,
    GetState,
    PiCommand,
    SetModel,
)
from .pi_events import AutoRetryEnd, DurablePiEvent, ModelRequestProgress, RetryEnd
from .pi_payloads import PiResponseData
from .pi_vocabulary import ThinkingLevelField
from .selected_session import SelectedSession
from .transcript_sources import TranscriptSource
from .turn_inputs import TurnInboxReceiver
from .unstarted_runtime import UnstartedRuntime


@dataclass(frozen=True)
class DurableDatabase(SessionFiles, RuntimeLocation):
    path: str

    def __post_init__(self) -> None:
        if not Path(self.path).is_absolute():
            raise ValueError('Durable database path must be absolute')

    def require_forkable(self) -> None:
        raise ValueError('Durable database forking is not implemented')

    def history_source(self, root: Path, incarnation: Any) -> DurableTranscript:
        return DurableTranscript(self)

    @property
    def session(self) -> Path:
        return Path(self.path)

    @property
    def writer_lock(self) -> Path:
        return self.session.with_suffix(self.session.suffix + '.lock')

    @property
    def members(self) -> tuple[Path, ...]:
        path = self.session
        return (path, *(path.with_name(path.name + suffix) for suffix in ('-wal', '-shm', '-journal')), self.writer_lock)

    def storage_files(self) -> tuple[SessionFiles, ...]:
        return (self,)

    def select_session(self, directory: Path, execution: Any) -> DurableSelectedSession:
        return DurableSelectedSession(directory, self)


@dataclass(frozen=True)
class DurableSelectedSession(SelectedSession):
    location: DurableDatabase

    @property
    def path(self) -> Path:
        return self.location.session

    @property
    def session_file(self) -> str:
        return self.location.path

    def verify_start(self, actual: NativeSessionIdentity) -> Path:
        actual.require_session(self.location.path)
        return self.path

    def require_context(self, context: ContextProof) -> None:
        self.verify_start(NativeSessionIdentity(context.session_id, str(context.session_file)))


@dataclass(kw_only=True)
class DurableChild(PiChild):
    location: DurableDatabase


@dataclass(frozen=True)
class HistoryData(PiResponseData):
    items: tuple[StoredEntry, ...]
    cursor: dict[str, JsonValue] | None = None


def _history_request(source: DurableTranscript, command: Entries) -> HistoryData:
    with source.reading() as reader:
        return reader.execute(reader, command)


@dataclass(frozen=True)
class DurableTranscript(TranscriptSource):
    location: DurableDatabase
    execute: Callable[[DurableTranscript, Entries], HistoryData] = field(default=_history_request, repr=False, compare=False)

    @contextmanager
    def reading(self) -> Iterator[DurableTranscript]:
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

    def cut(self) -> ExternalCut:
        return ExternalCut(max((entry.id for entry in self.execute(self, Entries(1)).items), default=0))

    def records(self, minimum: int, maximum: int, order: str) -> Iterator[StoredRecord]:
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

    def forward(self, after: int, through: int) -> Iterator[StoredRecord]:
        return self.records(after + 1, through, 'ascending')

    def reverse(self, before: int) -> Iterator[StoredRecord]:
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


class DurableRequestProgress(ModelRequestProgress, DurablePiEvent):
    wire_kind = ModelRequestProgress.wire_kind


@dataclass(frozen=True)
class DurableIdentityData(PiResponseData):
    session_id: str = field(metadata={'wire_name': 'sessionId'})


class GetIdentity(HostQuery):
    response_payload = DurableIdentityData


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
class Submit(HostQuery, TurnInboxItem):
    request_id: str = field(metadata={'wire_name': 'requestId'})
    text: str
    images: tuple[ImageInput, ...] = ()
    when_busy: WhenBusy = field(default=WhenBusy.STEER, metadata={'wire_name': 'whenBusy'})
    response_payload = SubmissionData

    @property
    def carried_inputs(self):
        return (InputId(self.request_id),)

    @property
    def public_input_id(self):
        return self.request_id

    def written(self, turn):
        pass

    def deliver_to(self, receiver, turn):
        return receiver.submit(self)


class SubmitInitial(Submit):
    @property
    def carried_inputs(self):
        return ()

    @property
    def public_input_id(self):
        return None

    def written(self, turn):
        turn.initial_written.set()


class AwaitTurn(HostQuery):
    pass


class DurableInputStarted(DurablePiEvent, declared_name=None):

    async def during_turn(self, turn):
        if not turn.delivery.input_started(self.id, self.native_id, self.text):
            await turn.child.request(Abort())
            raise RuntimeError('Core refused a started durable input')


@dataclass(frozen=True, kw_only=True)
class DurableOriginalStarted(DurableInputStarted, OriginalInputStarted):
    native_id: str
    text: str

    def convert(self):
        return (self,)


@dataclass(frozen=True, kw_only=True)
class DurableFollowingStarted(DurableInputStarted, FollowingInputStarted):
    native_id: str
    text: str

    def convert(self):
        return (self,)


@dataclass(frozen=True)
class DurableProofData:
    context: NativeContextRecord
    evidence_entry_id: str


@dataclass(frozen=True, kw_only=True)
class DurableDone(Done):
    proof: DurableProofData

    def selected_result(self, path: Path) -> NativeTurnResult:
        record = self.proof.context
        proof = DurableContextProof(record.input_id, record.session_id, record.session_entry_id,
            record.request_generation, record.llm_context_digest, record.committed_at_ms,
            path, self.proof.evidence_entry_id)
        return NativeTurnResult(self.text, proof)


@dataclass(frozen=True, kw_only=True)
class DurableTurnEnd(DurablePiEvent):
    text: str
    proof: DurableProofData

    def convert(self):
        return (DurableDone(text=self.text, ok=True, proof=self.proof),)

    async def finished_turn(self, completion):
        await completion
        return True


@dataclass(frozen=True, kw_only=True)
class DurableTurnFailed(DurablePiEvent):
    text: str
    reason: str

    def convert(self):
        return (Error(self.reason), Done(text=self.text, ok=False, reason_code=self.reason))

    async def finished_turn(self, completion):
        await completion
        return True


class DurableTurn(TurnInboxReceiver):
    """One Core turn's ingress and its existing child, not another transport."""

    def __init__(self, child, delivery, barrier, input_id, tool_socket, directory):
        self.child, self.delivery, self.barrier = child, delivery, barrier
        self.input_id, self.tool_socket = input_id, tool_socket
        self.tool_directory, self.tool_environment = directory, delivery.environment
        self.queue = delivery.inbox
        self.initial_written = asyncio.Event()
        self.rejected = []

    async def write(self, session, *commands):
        for command in commands:
            await self.child.request(command)

    def authority_changed(self, session):
        return asyncio.sleep(0, result=False)

    async def submit(self, command):
        command = replace(command, id=uuid4().hex)
        def write():
            self.child.write(command)
            command.written(self)
        async with self.barrier.ingress_async() as status:
            with status.phase.send_running(self.delivery.send_boundary, command.public_input_id,
                                           command.request_id, command.text) as outcome:
                completion = outcome.dispatch(self, self.child.proc, command, write)
        return await completion

    def refuse(self, turn, input_ids):
        self.rejected.extend(FollowingInputRefused(id=item.value) for item in input_ids)

    async def place(self, turn, item):
        return await self.submit(Submit(request_id=item.input_id.value,
            text=item.content.text, images=item.content.images, when_busy=item.when_busy))


@dataclass(frozen=True)
class OutcomeData(PiResponseData):
    outcome: CompactionOutcome


class CompactDurable(Compact):
    strict_response = True
    response_payload: ClassVar[type[PiResponseData]] = OutcomeData


class PiDurableBackend(PiCapabilities, AgentBackend, Steers):
    location_type = DurableDatabase
    unstarted_type = UnstartedRuntime
    event_kinds = frozenset(kind for member in DurablePiEvent.members_with(DurablePiEvent) for kind in member.known_kinds())

    def __init__(self, root):
        from .comms import Comms

        self.comms = Comms(root=root)
        self.child: DurableChild | None = None
        self.lock = asyncio.Lock()
        self.owner = None
        self.barrier = MaintenanceBarrier(root / 'registry.json')

    @classmethod
    def for_worker(cls, runner, session_id):
        return cls(runner.comms.root)

    @classmethod
    def registered_location(cls, root, worktree):
        return DurableDatabase(str(Path(root).absolute() / 'durable-sessions' / f'{uuid4().hex}.sqlite3'))

    async def open(self, thread, *, environment):
        async with self.lock:
            if self.child is None:
                location = thread.runtime
                path = location.session
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with ExitStack() as resources:
                    lock = resources.enter_context(location.writer_lock.open('a+b'))
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    child = await DurableChild.start_command(('node', str(Path(__file__).with_name('pi_durable_host.mjs'))),
                        cwd=thread.worktree, env=environment, pass_fds=(lock.fileno(),), event_type=DurablePiEvent,
                        resources=resources.pop_all(), location=location)
                async with child.failures():
                    model = SetModel.from_name(thread.model)
                    await child.request(Open(str(path), thread.worktree, model.provider, model.model_id, thread.thinking_level, str(RestartEnvironment.inherit(environment).native_config / 'auth.json')))
                    await child.request(Resume())
                self.child = child
            return self.child

    async def close_idle(self):
        async with self.lock:
            if self.child is not None:
                await self.child.shutdown()
                self.child = None

    async def inspect(self, thread, command):
        session = thread.runtime.select_session(self.comms.root / 'native-sessions' / thread.name, None)
        registry = await Coordination.run_worker(self.comms.registry.snapshot)
        environment, _ = session.launch_environment(session.directory,
            thread.native_environment(self.comms.root, registry, thread.worktree))
        return await (await self.open(thread, environment=environment)).request(command)

    def compaction_request(self, instructions):
        return CompactDurable(custom_instructions=instructions)

    async def run_turn(self, thread, content, delivery):
        session = thread.runtime.select_session(self.comms.root / 'native-sessions' / thread.name, None)
        environment, _ = session.launch_environment(session.directory, delivery.environment)
        child = await self.open(thread, environment=environment)
        async with AsyncExitStack() as resources:
            turn = DurableTurn(child, delivery, self.barrier, delivery.request_id(),
                               ToolTransport(), child.location.session.parent)
            mode = delivery.selected_tool_mode
            turn.tool_socket = await mode.open_transport(turn, resources)
            turn.tool_socket.bind_process(child.proc.pid)
            await self.configure_tools(child, mode, turn.tool_socket)
            identity = await child.request(GetIdentity())
            await delivery.prepare_identity(NativeSessionIdentity(identity.session_id, str(child.location.session)))
            async for event in self._stream_turn(thread, content, turn):
                yield event
            turn.tool_socket.assert_complete()

    async def _stream_turn(self, thread, content, turn):
        child = turn.child
        state = await child.request(GetState())
        yield AgentInfo(model=state.model.display_name,
            thinking_level=thread.thinking_level, session_file=child.location.path,
            context_size=state.model.context_window)
        self.owner = asyncio.current_task()
        try:
            async with child.failures(), child.reader.turn(), asyncio.TaskGroup() as tasks:
                await child.request(Watch())
                try:
                    child.forward_input(lambda: turn.forward(turn), create_task=tasks.create_task)
                    if not await turn.submit(SubmitInitial(request_id=turn.input_id,
                            text=content.text, images=content.images)):
                        raise IdentityConflict('Thread changed before Durable input write')
                    await turn.initial_written.wait()
                    completion = asyncio.create_task(child.request(AwaitTurn()))
                    try:
                        while (event := await child.reader.next_event()) is not None:
                            event.observe_request(partial(turn.delivery.request_progress, native_process=child.proc.identity))
                            await event.during_turn(turn)
                            for refused in turn.rejected:
                                yield refused
                            turn.rejected.clear()
                            for update in event.convert():
                                yield update
                            if await event.finished_turn(completion):
                                return
                        raise EOFError('Durable host ended before turn settlement')
                    finally:
                        completion.cancel()
                        await asyncio.gather(completion, return_exceptions=True)
                finally:
                    try:
                        await child.request(Unwatch())
                    finally:
                        await child.finish_input()
        finally:
            self.owner = None

    async def abort(self, owner):
        if self.owner is owner and self.child is not None:
            await self.child.request(Abort())
