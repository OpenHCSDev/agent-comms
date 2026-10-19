"""Shared Core inbox delivery and process lifetime for external CLI adapters."""
from __future__ import annotations

import argparse
import builtins
import asyncio
import json
import os
import secrets
import signal
import sys
import traceback
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, field, replace
from functools import cached_property, partial
from pathlib import Path
from typing import ClassVar, Generic, Literal, Self, TypeVar
from acp.schema import SessionConfigSelectOption
from metaclass_registry import RegistryConfig
from pydantic import Field, TypeAdapter, model_validator

from agent_comms.agent_runtime import DeliversInput, ExternalRuntime, PublishesTranscript
from agent_comms.change_report import ChangeReport
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
from agent_comms.config_options import CatalogConfigOption, CatalogSelection, ConfigOptions
from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_errors import CoordinationError
from agent_comms.declared_family import DeclaredFamily
from agent_comms import field_codec
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.message_reference import MessageReference
from agent_comms import messages
from agent_comms.read_basis import ExplicitWatchAcknowledgment
from agent_comms.runtime_updates import RuntimeUpdate, ThreadOutput
from agent_comms.send_result import SendResult
from agent_comms.typed_table import Column, TypedTable
from agent_comms_hooks.watch import WatchClaim, WatchRecord


@dataclass(frozen=True)
class Binding(TypedTable):
    root: str
    bus: str = field(metadata={'wire_name': 'bus_thread'})
    thread: str = field(metadata={'wire_name': 'session'})

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        group = parser.add_argument_group('Core/CLI binding (all or none)')
        for _, key in field_codec.FieldCodec.record_fields(cls):
            group.add_argument('--' + key.replace('_', '-'))

    @classmethod
    def from_arguments(cls, arguments: argparse.Namespace) -> Binding | None:
        values = {key: vars(arguments)[key] for _, key in field_codec.FieldCodec.record_fields(cls)
                  if vars(arguments)[key] is not None}
        return field_codec.FieldCodec.decode(cls, {cls.family_discriminator: cls.declared_name, **values}) if values else None

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
        arguments = parser.parse_args(namespace=SendArguments())
        arguments.operation = arguments.operation.from_arguments(arguments)
        arguments.binding = arguments.operation.binding(arguments)
        return cls(**{declared.name: vars(arguments)[key]
                      for declared, key in field_codec.FieldCodec.record_fields(cls)})

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
    def message(self) -> messages.Message:
        return messages.Message.from_wire(json.loads(self.payload))

    async def finish(self, bridge: CliBridge, outcome: type[Outcome]) -> None:
        with bridge.db:
            replace(self, outcome=outcome).upsert(bridge.db)
        await outcome.acknowledge(bridge, self.message)

BridgeOptions = TypeVar('BridgeOptions', bound=Options)
OperationBridge = TypeVar('OperationBridge', bound='CliBridge')


class DeclaredOptions:
    """The CLI adapter consumes FieldCodec's existing field plans."""
    help: ClassVar[str]

    @classmethod
    def add_parser(cls, subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        cls.add_arguments(subparsers.add_parser(field_codec.FieldCodec.encode(cls), help=cls.help))

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        field_codec.FieldCodec.add_arguments(cls, parser)

    @classmethod
    def from_arguments(cls, args: argparse.Namespace) -> Self:
        return field_codec.FieldCodec.decode_arguments(cls, args)


@dataclass(frozen=True)
class Operation(DeclaredOptions, ABC, Generic[OperationBridge]):
    """A decoded CLI command owns its execution."""

    def binding(self, arguments: argparse.Namespace) -> Binding | None:
        return Binding.from_arguments(arguments)

    @abstractmethod
    async def execute(self, bridge: OperationBridge) -> None: ...

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


def _json_object(value: str) -> dict[str, object]:
    return field_codec.FieldCodec.decode(dict[str, object], field_codec.FieldCodec.parse(value))


@dataclass(frozen=True)
class TargetField:
    """The declared field owns parsing, editor hints and requiredness."""
    plan: field_codec.FieldPlan
    value: object

    @classmethod
    def of(cls, plan: field_codec.FieldPlan, value: object) -> TargetField:
        editor = CatalogTargetField if 'catalog' in plan.declared.metadata else plan.setting('editor', cls)
        return editor(plan, value)

    @property
    def annotation(self) -> object:
        return self.plan.annotation

    @property
    def name(self) -> str:
        return self.plan.key

    @property
    def multiline(self) -> bool:
        return self.plan.setting('multiline', False)

    @property
    def description(self) -> str:
        return self.plan.description

    @property
    def editor_default(self) -> object:
        return self.plan.editor_default(self.value)

    def editor_hints(self, choices: Callable[[ChoiceTargetField], object]) -> tuple[object, ...]:
        """An unrestricted field does not declare a finite-choice editor."""
        return ()

    async def prepare(self, catalogs: ConfigOptions, selection: CatalogSelection) -> TargetField:
        return self

    @property
    def required(self) -> bool:
        return self.plan.constructor_required

    def value_schema(self) -> dict[str, object]:
        return field_codec.FieldCodec.value_schema(self.annotation)

    def json_schema(self) -> dict[str, object]:
        return {**self.value_schema(), 'multiline': self.multiline,
                'description': self.description, 'editor_default': self.editor_default}


@dataclass(frozen=True)
class ChoiceTargetField(TargetField):
    """A prepared finite declaration, from its annotation or configuration catalog."""
    options: tuple[SessionConfigSelectOption, ...] = ()

    @property
    def editor_declarations(self) -> tuple[object, ...]:
        return ()

    @property
    def choices(self) -> tuple[tuple[str, str], ...]:
        return tuple((choice.name, choice.value) for choice in self.options)

    async def prepare(self, catalogs: ConfigOptions, selection: CatalogSelection) -> ChoiceTargetField:
        decoder = field_codec.compile_decoder(self.annotation)
        options = (SessionConfigSelectOption(name=label, value=decoder.encode_editor(decoder.decode(value)))
                   for label, value in decoder.editor_choices())
        return replace(self, options=tuple(options))

    def editor_hints(self, choices: Callable[[ChoiceTargetField], object]) -> tuple[object, ...]:
        return (choices(self),)


class CatalogTargetField(ChoiceTargetField):
    @property
    def catalog(self) -> type[CatalogConfigOption]:
        return self.plan.setting('catalog', CatalogConfigOption)

    @property
    def editor_declarations(self) -> tuple[object, ...]:
        return (self.catalog,)

    async def prepare(self, catalogs: ConfigOptions, selection: CatalogSelection) -> CatalogTargetField:
        catalog = catalogs.catalog_for(self.catalog)
        options = await catalog.display_choices(selection)
        decoder = field_codec.compile_decoder(self.annotation)
        return replace(self, options=tuple(SessionConfigSelectOption(name=item.name,
            value=decoder.encode_editor(decoder.decode(item.value))) for item in options))

    def value_schema(self) -> dict[str, object]:
        return {**field_codec.FieldCodec.value_schema(self.annotation), 'configuration': field_codec.FieldCodec.encode(self.catalog)}





class SendArguments(argparse.Namespace):
    command: str

class SendReport(field_codec.SubclassUnion, affix=""):
    def send(self, ctx: Comms, request: Send) -> SendResult:
        return self.emit(partial(ctx.messaging.send, request.sender, request.target,
                                 request.body, request.type, role=request.role()))

    @abstractmethod
    def emit(self, send: Callable[..., SendResult]) -> SendResult: ...


class MissingReport(field_codec.NullMember, SendReport):
    def emit(self, send: Callable[..., SendResult]) -> SendResult:
        return send()


@dataclass(frozen=True)
class SuppliedReport(ChangeReport, field_codec.RecordMember, SendReport):
    def emit(self, send: Callable[..., SendResult]) -> SendResult:
        return send(report=self)


@dataclass(frozen=True, kw_only=True)
class Send(Operation):
    help = "Send to a thread (DM), #channel, or #all"
    sender: str = field_codec.FieldCodec.option("--from", default="")
    target: str = field_codec.FieldCodec.option("--to")
    body: str = field_codec.FieldCodec.option("--body")
    type: builtins.type[messages.MessageKind] = field_codec.FieldCodec.option("--type", default=messages.InfoKind,
        choices=messages.SendableMessageKind.names(), editor=ChoiceTargetField)
    role: builtins.type[messages.SendableRole] = field_codec.FieldCodec.option("--role", default=messages.ConversationRole)
    report: SendReport = field_codec.FieldCodec.option("--report", default=MissingReport(),
        help="Change report fields as a JSON object", type=field_codec.FieldCodec.parse)

    async def execute(self, bridge: CliBridge) -> None:
        request = replace(self, sender=bridge.binding.bus)
        await bridge.show_sent(await asyncio.to_thread(request.report.send, bridge.comms, request))


class Inbox(Operation[OperationBridge]):
    @classmethod
    def serves(cls, bridge: type[CliBridge]) -> bool:
        """Delivery alone, without the transcript that confirms it on some bridges."""
        return not issubclass(bridge, TranscriptConfirmsDelivery)

    def start(self, bridge: OperationBridge, tasks: asyncio.TaskGroup) -> None:
        tasks.create_task(bridge.watch())

    async def execute(self, bridge: OperationBridge) -> None:
        # The claim records how this watch was started, so the session's hook can
        # name the re-arm command once the lease lapses.
        record = WatchRecord(bridge.binding.bus, bridge.binding.root,
                             (Path(sys.argv[0]).name, *sys.argv[1:]))
        with WatchClaim(bridge.binding.thread).held(record):
            from agent_comms.registration import Registration

            registry = await asyncio.to_thread(
                Registration, Path(bridge.binding.root) / 'registry.json')
            # Retry only endpoint acquisition, never the running bridge's delivery work.
            with ExitStack() as endpoint:
                await bridge.comms.bus.when_open(lambda: endpoint.enter_context(
                    registry.external_process(bridge.binding.bus, ProcessIdentity.capture(os.getpid()))))
                async with asyncio.TaskGroup() as tasks:
                    self.start(bridge, tasks)


class Watch(Inbox):
    @classmethod
    def serves(cls, bridge: type[CliBridge]) -> bool:
        return issubclass(bridge, PublishesTranscript)

    def start(self, bridge: TranscriptBridge, tasks: asyncio.TaskGroup) -> None:
        super().start(bridge, tasks)
        tasks.create_task(bridge.publish_transcript())


@dataclass(frozen=True)
class IncomingFrame(ABC):
    message: messages.Message

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

    async def accept(self, message: messages.Message) -> None:
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
            '--after', json.dumps(field_codec.FieldCodec.encode(DeliveryRow.resume_after(self.db))),
            '--acknowledgment', field_codec.FieldCodec.encode(ExplicitWatchAcknowledgment)),
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
            await self.options.operation.execute(self)
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
        bridge.confirm_probe(self)


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
    """Monitor delivery requires the same live transcript subscription."""

    incoming_frame: ClassVar[type[IncomingFrame]]
    window_bytes: ClassVar[int]
    PROBE_NOTICE_SECONDS: ClassVar[float] = 15.0

    def __init__(self, options: Options, binding: Binding, db: CoordinationStore) -> None:
        super().__init__(options, binding, db)
        self.probe = MonitorProbe(session=binding.thread, token=secrets.token_hex(24))
        self.monitor_ready = asyncio.Event()
        self.pending_parts: dict[tuple[str, int], int] = {}
        self.window_changed = asyncio.Condition()
        self.write_line = ExplicitWatchAcknowledgment.line_output(sys.stdout)

    async def prepare_delivery(self) -> None:
        # The probe is on stdout before the wait: no later write exists to flush it.
        print(self.probe.model_dump_json(), flush=True)
        print('Waiting for this session to observe the monitor probe; inbox remains unread.',
              file=sys.stderr, flush=True)
        # The harness owns notification scheduling. Missing proof never opens
        # the inbox; the watch's existing cancellation owns this wait's lifetime.
        try:
            await asyncio.wait_for(self.monitor_ready.wait(), self.PROBE_NOTICE_SECONDS)
        except TimeoutError:
            # A line-lagging filter (grep aliased to ugrep) holds the last line until another
            # arrives; this notice says the wait out loud and releases a held probe.
            print(f'Monitor probe not observed after {self.PROBE_NOTICE_SECONDS:g}s; '
                  'inbox remains unread until this session observes it.', flush=True)
            await self.monitor_ready.wait()

    def confirm_probe(self, probe: MonitorProbe) -> None:
        if probe == self.probe:
            self.monitor_ready.set()

    async def part_observed(self, message_id: str, number: int) -> None:
        async with self.window_changed:
            self.pending_parts.pop((message_id, number), None)
            self.window_changed.notify_all()

    @staticmethod
    def encode(text: str) -> str:
        # One reversible JSON string per line; no truncation or lost line breaks.
        return json.dumps(text, ensure_ascii=False)

    async def deliver(self, message: messages.Message) -> type[Outcome]:
        parts = self.incoming_frame(message).parts
        with self.db:
            DeliveryRow.update(self.db, where='id=?', parameters=(message.message_id,), monitor=ReceivingMonitor(len(parts)))
        for number, part in enumerate(parts, 1):
            line = self.encode(part)
            size = len((line + '\n').encode('utf-16-le'))
            async with self.window_changed:
                await self.window_changed.wait_for(
                    lambda: not self.pending_parts
                    or sum(self.pending_parts.values()) + size <= self.window_bytes)
                self.pending_parts[message.message_id, number] = size
                self.write_line(line)
        return EmittedOutcome

    async def show_sent(self, result: SendResult) -> None:
        self.write_line(self.encode(self.sent_text(result)))

    async def report_failure(self, text: str) -> None:
        self.write_line(self.encode(text))


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
