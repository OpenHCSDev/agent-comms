"""Shared Core inbox delivery and process lifetime for external CLI adapters."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import traceback
from abc import ABC, abstractmethod
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, field, replace
from functools import cached_property, partial
from pathlib import Path
from typing import Annotated, ClassVar, Generic, Literal, Self, TypeVar

from metaclass_registry import RegistryConfig
from pydantic import Field, TypeAdapter, model_validator

from agent_comms.agent_runtime import DeliversInput, ExternalRuntime, PublishesTranscript
from agent_comms.child_process import AttachedChild, ProcessIdentity, StreamingChildStdio
from agent_comms.cli_protocol import (
    DeliveredOutcome,
    EmittedOutcome,
    FilteredOutcome,
    Outcome,
    TaggedWire,
    UnconfirmedOutcome,
    WatchLine,
    raise_child_exit,
)
from agent_comms.comms import Comms
from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_errors import CoordinationError
from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec, PathText
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.message_reference import MessageReference
from agent_comms.messages import InfoKind, Message, MessageKind
from agent_comms.read_basis import ExplicitWatchAcknowledgment
from agent_comms.runtime_updates import RuntimeUpdate, ThreadOutput
from agent_comms.send_result import SendResult
from agent_comms.typed_table import Column, TypedTable
from agent_comms_hooks.watch import WatchClaim, WatchRecord
from .monitor import MonitorAttachment, MonitorService, read_monitor


@dataclass(frozen=True)
class Binding(TypedTable):
    root: str
    bus: str = field(metadata={'wire_name': 'bus_thread'})
    thread: str = field(metadata={'wire_name': 'session'})

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        group = parser.add_argument_group('Core/CLI binding (all or none)')
        for _, key in FieldCodec.record_fields(cls):
            group.add_argument('--' + key.replace('_', '-'))

    @classmethod
    def from_arguments(cls, arguments: argparse.Namespace) -> Binding | None:
        values = {key: vars(arguments)[key] for _, key in FieldCodec.record_fields(cls)
                  if vars(arguments)[key] is not None}
        return FieldCodec.decode(cls, {cls.family_discriminator: cls.declared_name, **values}) if values else None

    @classmethod
    @contextmanager
    def open(cls, state: Path):
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(CoordinationStore.writer(state / 'delivery.sqlite3')) as db:
            db.execute('PRAGMA synchronous=FULL')
            for row in (cls, DeliveryRow, ObservedPart):
                row.create(db, if_not_exists=True)
            db.commit()
            yield db

    @classmethod
    def bind(cls, db: CoordinationStore, requested: Binding | None) -> Binding:
        with db:
            if requested is not None:
                db.execute('BEGIN IMMEDIATE')
            rows = cls.select(db)
            if not rows and requested is not None:
                requested.insert(db)
                return requested
            if len(rows) != 1:
                raise ValueError('Supply one complete Core/CLI binding for this state directory')
            current = rows[0]
            if requested is not None and requested != current:
                raise ValueError('This state directory belongs to another Core/CLI conversation')
            return current


@dataclass
class Options:
    operation: Operation
    binding: Binding | None
    state: Path

    @classmethod
    def parse(cls, description: str | None, state: Path, bridge_type: type[CliBridge]) -> Self:
        common = argparse.ArgumentParser(add_help=False)
        Binding.add_arguments(common)
        common.add_argument('--state', type=Path, default=state)
        bridge_type.add_arguments(common)
        parser = argparse.ArgumentParser(description=description)
        commands = parser.add_subparsers(required=True)
        for operation in Operation.offered(bridge_type):
            command = commands.add_parser(operation.__name__.lower(), parents=[common])
            operation.add_arguments(command)
            command.set_defaults(operation=operation)
        arguments = parser.parse_args()
        arguments.operation = arguments.operation.from_arguments(arguments)
        arguments.binding = arguments.operation.binding(arguments)
        return cls(**{declared.name: vars(arguments)[key]
                      for declared, key in FieldCodec.record_fields(cls)})

@dataclass(frozen=True)
class MonitorEvidence(DeclaredFamily):
    """Unsent and terminal observations cannot accept another delivery."""

    async def confirm(self, bridge: LineMonitorBridge, delivery: DeliveryRow,
                      notification: MonitorPart, record: ObservedMonitor) -> None:
        pass


@dataclass(frozen=True)
class UnrecordedMonitor(MonitorEvidence):
    pass


@dataclass(frozen=True)
class ReceivingMonitor(MonitorEvidence):
    total: int

    async def confirm(self, bridge: LineMonitorBridge, delivery: DeliveryRow,
                      notification: MonitorPart, record: ObservedMonitor) -> None:
        if notification.total != self.total:
            return
        part = ObservedPart(delivery.id, notification.number, notification.sender,
                            notification.body, record.record_id, record.timestamp)
        with bridge.db:
            part.insert_if_absent(bridge.db)
        await bridge.part_observed(delivery.id, notification.number)
        if ObservedPart.count(bridge.db, id=delivery.id) != self.total:
            return
        parts = ObservedPart.select(bridge.db, id=delivery.id, order_by=('number',))
        message = delivery.message
        if (''.join(part.body for part in parts) != message.body
                or any(part.sender != message.sender for part in parts)):
            with bridge.db:
                replace(delivery, monitor=MismatchedMonitor('Observed parts do not equal the original message')).upsert(bridge.db)
            print(f'Monitor parts mismatch; message remains pending: {delivery.id}', file=sys.stderr, flush=True)
            return
        await replace(delivery, monitor=record).finish(bridge, DeliveredOutcome)


@dataclass(frozen=True)
class MismatchedMonitor(MonitorEvidence):
    reason: str


@dataclass(frozen=True)
class ObservedMonitor(MonitorEvidence):
    record_id: str
    timestamp: float


@dataclass(frozen=True)
class ObservedPart(TypedTable):
    id: str = field(metadata={'sql': Column(primary_key=True)})
    number: int = field(metadata={'sql': Column(primary_key=True)})
    sender: str
    body: str
    record_id: str
    timestamp: float


@dataclass(frozen=True)
class DeliveryRow(TypedTable):
    sequence_sql: ClassVar[str] = "json_extract(payload, '$.seq')"

    @classmethod
    def schema_objects(cls, *, if_not_exists: bool = False):
        objects = super().schema_objects(if_not_exists=if_not_exists)
        existence = 'IF NOT EXISTS ' if if_not_exists else ''
        objects[f'{cls.declared_name}_sequence'] = (
            f'CREATE INDEX {existence}"{cls.declared_name}_sequence" '
            f'ON "{cls.declared_name}" ({cls.sequence_sql})')
        return objects

    @classmethod
    def resume_after(cls, db: CoordinationStore) -> MessageReference | None:
        """The existing journal owns attempted progress, including uncertain emissions."""
        latest = cls.read(db.execute(
            f'SELECT {cls._column_list(tuple(cls.column_projection()))} FROM "{cls.declared_name}" '
            f'ORDER BY {cls.sequence_sql} DESC LIMIT 1'))
        return latest[0].message.reference if latest else None

    id: str = field(metadata={'sql': Column(primary_key=True)})
    payload: str
    outcome: type[Outcome] = field(metadata={'sql': Column(index=True)})
    monitor: MonitorEvidence = field(default_factory=UnrecordedMonitor)

    @cached_property
    def message(self) -> Message:
        return Message.from_wire(json.loads(self.payload))

    async def finish(self, bridge: CliBridge, outcome: type[Outcome]) -> None:
        outcome = self.outcome.followed_by(outcome)
        with bridge.db:
            replace(self, outcome=outcome).upsert(bridge.db)
        await outcome.acknowledge(bridge, self.message)

BridgeOptions = TypeVar('BridgeOptions', bound=Options)
OperationBridge = TypeVar('OperationBridge', bound='CliBridge')


@dataclass(frozen=True)
class Operation(ABC, Generic[OperationBridge]):
    """A decoded CLI command owns its execution."""

    def binding(self, arguments: argparse.Namespace) -> Binding | None:
        return Binding.from_arguments(arguments)

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        """Declare the options only this command takes."""

    @classmethod
    def from_arguments(cls, arguments: argparse.Namespace) -> Operation:
        return FieldCodec.decode(cls, {key: vars(arguments)[key] for _, key in FieldCodec.record_fields(cls)})

    @abstractmethod
    async def run(self, bridge: OperationBridge) -> None: ...

    def transcript_source(self) -> Path:
        raise ValueError('This command does not own a live transcript subscription')

    @classmethod
    def serves(cls, bridge: type[CliBridge]) -> bool:
        """Whether this command runs on that bridge; every bridge sends."""
        return True

    @classmethod
    def offered(cls, bridge: type[CliBridge]) -> tuple[type[Operation], ...]:
        """The commands a bridge's CLI takes, derived from what the bridge can do."""
        pending, offered = list(cls.__subclasses__()), []
        while pending:
            operation = pending.pop(0)
            pending.extend(operation.__subclasses__())
            if operation.serves(bridge):
                offered.append(operation)
        return tuple(offered)


@dataclass(frozen=True)
class Send(Operation):
    target: str = field(metadata={'wire_name': 'to'})
    body: str
    kind: type[MessageKind] = field(metadata={'wire_name': 'type'})

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        parser.add_argument('--to', required=True)
        parser.add_argument('--body', required=True)
        parser.add_argument('--type', default=FieldCodec.encode(InfoKind), choices=MessageKind.sendable_names())

    async def run(self, bridge: CliBridge) -> None:
        result = await asyncio.to_thread(bridge.comms.messaging.send, bridge.binding.bus,
                                         self.target, self.body, self.kind)
        await bridge.show_sent(result)


class Inbox(Operation[OperationBridge]):
    @classmethod
    def serves(cls, bridge: type[CliBridge]) -> bool:
        """Delivery alone, without the transcript that confirms it on some bridges."""
        return not issubclass(bridge, TranscriptConfirmsDelivery)

    def start(self, bridge: OperationBridge, tasks: asyncio.TaskGroup) -> None:
        tasks.create_task(bridge.watch())

    async def run(self, bridge: OperationBridge) -> None:
        await bridge.registered_watch(self)


class Watch(Inbox):
    @classmethod
    def serves(cls, bridge: type[CliBridge]) -> bool:
        return issubclass(bridge, PublishesTranscript)

    async def run(self, bridge: OperationBridge) -> None:
        await bridge.watch_operation(self)

    def start(self, bridge: TranscriptBridge, tasks: asyncio.TaskGroup) -> None:
        super().start(bridge, tasks)
        tasks.create_task(bridge.publish_transcript())


@dataclass(frozen=True)
class Serve(Watch):
    owner: ProcessIdentity
    transcript: Annotated[Path, PathText]

    @classmethod
    def serves(cls, bridge: type[CliBridge]) -> bool:
        return issubclass(bridge, LineMonitorBridge)

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        parser.add_argument('--owner', type=json.loads, required=True)
        parser.add_argument('--transcript', required=True)

    def transcript_source(self) -> Path:
        return self.transcript

    async def run(self, bridge: LineMonitorBridge) -> None:
        if self.transcript.stem != bridge.binding.thread:
            raise ValueError('The live transcript belongs to another Claude session')
        self.owner.require_ancestor_of(os.getpid())
        await bridge.registered_watch(self)

    def start(self, bridge: LineMonitorBridge, tasks: asyncio.TaskGroup) -> None:
        super().start(bridge, tasks)
        current = asyncio.current_task()
        assert current is not None
        tasks.create_task(MonitorService(bridge, self.owner, current).run())


class Enrol(Operation):
    @classmethod
    def serves(cls, bridge: type[CliBridge]) -> bool:
        return issubclass(bridge, LineMonitorBridge)

    async def run(self, bridge: LineMonitorBridge) -> None:
        WatchClaim(bridge.binding.thread).enrol(bridge.watch_record())
        print('Session binding enrolled; SessionStart owns the bridge. Monitor runs watch.', flush=True)


@dataclass(frozen=True)
class IncomingFrame(ABC):
    message: Message

    @property
    def text(self) -> str:
        origin = self.message.sender_role.origin_label
        return (f'Comms in · {self.message.sender} ({origin})\n'
                f'{self.message.body}\nMessage ID: {self.message.message_id}')

class CliBridge(ExternalRuntime, DeliversInput, Generic[BridgeOptions]):
    """Core delivery and lifetime shared by CLI-specific transports."""

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        """Declare the options this harness's transport needs."""

    def __init__(self, options: BridgeOptions, binding: Binding, db: CoordinationStore) -> None:
        self.options = options
        self.binding = binding
        self.db = db

    @cached_property
    def comms(self) -> Comms:
        return Comms(Path(self.binding.root))

    def watch_record(self) -> WatchRecord:
        return WatchRecord(self.binding.bus, self.binding.root,
                           (Path(sys.argv[0]).name, *sys.argv[1:]))

    async def registered_watch(self, operation: Inbox) -> None:
        from agent_comms.registration import Registration
        registry = await asyncio.to_thread(
            Registration, Path(self.binding.root) / 'registry.json')
        with ExitStack() as endpoint:
            await self.comms.bus.when_open(lambda: endpoint.enter_context(
                registry.external_process(self.binding.bus, ProcessIdentity.capture(os.getpid()))))
            with WatchClaim(self.binding.thread).held(self.watch_record()):
                async with asyncio.TaskGroup() as tasks:
                    operation.start(self, tasks)

    async def watch_operation(self, operation: Watch) -> None:
        await self.registered_watch(operation)

    @abstractmethod
    async def show_sent(self, result: SendResult) -> None:
        """Display a completed send without publishing it again."""

    @abstractmethod
    async def report_failure(self, text: str) -> None:
        """Report a bridge failure through the CLI's visible output."""

    def sent_text(self, result: SendResult) -> str:
        message = result.message
        return '\n'.join((f'Comms out · {message.sender} → {message.target}',
                          message.body, f'Message ID: {message.message_id}', *result.notices))

    async def prepare_delivery(self) -> None:
        """API transports already bind the intended harness on each request."""

    async def accept(self, message: Message) -> None:
        conversation = message.type.conversation_message
        outcome = UnconfirmedOutcome if conversation else FilteredOutcome
        delivery = DeliveryRow(message.message_id, json.dumps(message.to_wire(), separators=(',', ':')) + '\n', outcome)
        with self.db:
            first = delivery.insert_if_absent(self.db).rowcount
        if not first:
            delivery, = DeliveryRow.select(self.db, id=message.message_id)
            await delivery.outcome.acknowledge(self, message)
            return
        result = await self.deliver(message) if conversation else outcome
        delivery, = DeliveryRow.select(self.db, id=message.message_id)
        await delivery.finish(self, result)
        print(f'{result.declared_name}: {message.message_id} from {message.sender}', file=sys.stderr, flush=True)

    async def watch(self) -> None:
        await self.prepare_delivery()
        print(f'Watching {self.binding.bus} → {self.binding.thread}; unconfirmed attempts retained without replay', file=sys.stderr, flush=True)
        await self.comms.bus.when_open(self.watch_child)

    async def watch_child(self) -> None:
        """One child stream; the bus owns waiting and its next attempt after a hold."""
        child = await AttachedChild.start((
            str(LaunchRuntime.running().interpreter.with_name('agent-comms')),
            '--root', self.binding.root, 'watch', '--thread', self.binding.bus,
            '--after', json.dumps(FieldCodec.encode(DeliveryRow.resume_after(self.db))),
            '--acknowledgment', FieldCodec.encode(ExplicitWatchAcknowledgment)),
            stdio=StreamingChildStdio(input_enabled=False))
        try:
            async for item in WatchLine.stream(child):
                await item.handle(self, child)
            await raise_child_exit(child, '')
        finally:
            await child.stop()

    async def run(self) -> None:
        current = asyncio.current_task()
        assert current is not None
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, current.cancel)
        try:
            await self.options.operation.run(self)
        except* BlockingIOError:
            raise
        except* (OSError, ValueError, CoordinationError) as group:
            failure = 'Comms bridge stopped:\n' + ''.join(
                traceback.TracebackException.from_exception(
                    group, max_group_width=sys.maxsize, max_group_depth=sys.maxsize,
                ).format()
            )
            print(failure, file=sys.stderr, flush=True)
            await self.report_failure(failure + '\nNo message was retried.')
            raise


class TranscriptBridge(PublishesTranscript, CliBridge[BridgeOptions]):
    """A CLI with an event subscription also publishes whole transcript items."""

    async def publish_transcript(self) -> None:
        comms = self.comms
        output = ThreadOutput(
            await asyncio.to_thread(comms.transcripts.external, self.binding.bus), comms.agents)
        # This subscription can assemble only items whose start it receives.
        # A prefix from the previous subscription cannot stand for that stream.
        await comms.bus.when_open(output.transcript.end_turn)
        # Each CLI reports its own initial state. Disk writes never stop the
        # event reader from receiving a newer whole draft of the same item.
        queue: asyncio.Queue[RuntimeUpdate] = asyncio.Queue()

        async def read() -> None:
            try:
                async for update in self.transcript():
                    await update.observed(self)
                    queue.put_nowait(update)
            finally:
                queue.shutdown()

        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(read())
            while True:
                try:
                    pending = [await queue.get()]
                except asyncio.QueueShutDown:
                    break
                while not queue.empty():
                    pending.append(queue.get_nowait())
                await comms.bus.when_open(partial(output.publish_batch, pending))

class MonitorFrame(TaggedWire, ABC):
    """Our line protocol inside the harness's task-notification envelope."""
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='wire_tag', skip_if_no_key=True)
    @abstractmethod
    async def confirm(self, bridge: LineMonitorBridge, record: ObservedMonitor) -> None: ...


class MonitorProbe(MonitorFrame):
    wire_tag = 'probe'
    protocol: Literal['agent-comms-monitor'] = 'agent-comms-monitor'
    wire_prefix: ClassVar[str] = '{"protocol":' + json.dumps(protocol) + ','
    session: str
    token: str

    async def confirm(self, bridge: LineMonitorBridge, record: ObservedMonitor) -> None:
        await bridge.confirm_probe(self)


class MonitorPart(MonitorFrame):
    wire_tag = 'part'
    id: str
    sender: str
    number: int = Field(ge=1)
    total: int = Field(ge=1)
    body: str

    @model_validator(mode='after')
    def valid_number(self):
        if self.number > self.total:
            raise ValueError('Monitor part number exceeds total')
        return self

    async def confirm(self, bridge: LineMonitorBridge, record: ObservedMonitor) -> None:
        for delivery in DeliveryRow.select(bridge.db, id=self.id):
            await delivery.monitor.confirm(bridge, delivery, self, record)


class ForeignMonitorFrame(MonitorFrame):
    wire_tag = 'foreign'
    text: str

    async def confirm(self, bridge: LineMonitorBridge, record: ObservedMonitor) -> None:
        """Another monitor's output is not comms delivery evidence."""


MONITOR_FRAME = TypeAdapter(MonitorFrame.declared_union())


@dataclass(frozen=True)
class MonitorObservation(RuntimeUpdate):
    frame: MonitorFrame
    record: ObservedMonitor

    async def observed(self, bridge: LineMonitorBridge) -> None:
        await self.frame.confirm(bridge, self.record)

    def publish(self, output: ThreadOutput) -> None:
        """Delivery is not a second conversation item."""


class TranscriptConfirmsDelivery:
    """Delivery is confirmed only by observing it in the bridge's own transcript subscription."""


class LineMonitorBridge(TranscriptConfirmsDelivery, TranscriptBridge[Options]):
    """Session-owned subscription and delivery journal, with replaceable readers."""

    incoming_frame: ClassVar[type[IncomingFrame]]
    window_bytes: ClassVar[int]

    def __init__(self, options: Options, binding: Binding, db: CoordinationStore) -> None:
        super().__init__(options, binding, db)
        self.reader: MonitorAttachment | None = None
        self.reader_changed = asyncio.Condition()

    def watch_record(self) -> WatchRecord:
        command = (str(Path(sys.argv[0]).absolute()), Watch.__name__.lower(),
                   '--state', str(self.options.state.absolute()))
        return WatchRecord(self.binding.bus, self.binding.root, command)

    async def watch_operation(self, operation: Watch) -> None:
        await read_monitor(WatchClaim(self.binding.thread))

    async def accept(self, message: Message) -> None:
        # Admission precedes DeliveryRow persistence. Detachment never creates
        # an attempt for a message that has not reached an attached reader.
        await self.prepare_delivery()
        await super().accept(message)

    async def prepare_delivery(self) -> None:
        async with self.reader_changed:
            await self.reader_changed.wait_for(
                lambda: self.reader is not None and self.reader.ready.is_set()
                and not self.reader.writer.is_closing())

    async def confirm_probe(self, probe: MonitorProbe) -> None:
        async with self.reader_changed:
            if self.reader is not None and probe == self.reader.probe:
                self.reader.ready.set()
                self.reader_changed.notify_all()
                print('Fresh Monitor probe observed; delivery admitted.', file=sys.stderr, flush=True)

    async def part_observed(self, message_id: str, number: int) -> None:
        if self.reader is not None:
            await self.reader.observed(message_id, number)

    @staticmethod
    def encode(text: str) -> str:
        # One reversible JSON string per line; no truncation or lost line breaks.
        return json.dumps(text, ensure_ascii=False)

    async def deliver(self, message: Message) -> type[Outcome]:
        attachment = self.reader
        if attachment is None or not attachment.ready.is_set():
            raise RuntimeError('Monitor delivery was not admitted')
        parts = self.incoming_frame(message).parts
        with self.db:
            DeliveryRow.update(self.db, where='id=?', parameters=(message.message_id,), monitor=ReceivingMonitor(len(parts)))
        try:
            for number, part in enumerate(parts, 1):
                await attachment.emit(message.message_id, number, self.encode(part), self.window_bytes)
        except ConnectionError:
            # Some bytes may already have reached the tool. Preserve this attempt
            # as UNKNOWN; a later reader must never replay it.
            print(f'Monitor detached during {message.message_id}; attempt preserved, no replay.',
                  file=sys.stderr, flush=True)
            return UnconfirmedOutcome
        return EmittedOutcome

    async def show_sent(self, result: SendResult) -> None:
        print(self.encode(self.sent_text(result)), flush=True)

    async def report_failure(self, text: str) -> None:
        print(text, file=sys.stderr, flush=True)


def run_bridge(bridge_type: type[CliBridge[BridgeOptions]], options: BridgeOptions) -> None:
    os.umask(0o077)
    try:
        with Binding.open(options.state) as db, asyncio.Runner() as runner:
            bridge = bridge_type(options, Binding.bind(db, options.binding), db)
            runner.run(bridge.run())
    except* asyncio.CancelledError:
        pass
    except* BlockingIOError:
        sys.exit('Another watcher owns this conversation')
