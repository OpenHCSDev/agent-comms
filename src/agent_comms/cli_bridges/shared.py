"""Shared Core inbox delivery and process lifetime for external CLI adapters."""
from __future__ import annotations

import argparse
import asyncio
import builtins
import io
import json
import os
import signal
import socket
import sys
import time
import traceback
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import ExitStack, closing, contextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import cached_property, partial
from pathlib import Path
from typing import Annotated, ClassVar, Generic, Literal, Self, TextIO, TypeVar

from acp.schema import SessionConfigSelectOption
from metaclass_registry import RegistryConfig
from pydantic import Field, TypeAdapter, model_validator

from agent_comms import field_codec, messages
from agent_comms.agent_runtime import DeliversInput, ExternalRuntime, PublishesTranscript
from agent_comms.change_report import ChangeReport
from agent_comms.child_process import AttachedChild, ProcessIdentity, SpawnedProcess, StreamingChildStdio
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
from agent_comms.field_codec import FieldCodec, PathText, option
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.message_reference import MessageReference
from agent_comms.read_basis import ExplicitWatchAcknowledgment
from agent_comms.runtime_updates import RuntimeUpdate, ThreadOutput
from agent_comms.send_result import SendResult
from agent_comms.typed_table import Column, DurableTables, ExpressionIndex, TypedTable
from agent_comms_hooks.watch import WatchClaim, WatchRecord

from ..locked_store import RegistryFile, SQLiteFile
from .monitor import (
    REPLIES,
    BridgeExit,
    InspectSession,
    MonitorAttachment,
    MonitorPeer,
    MonitorService,
    NoReader,
    PauseSession,
    ReaderState,
    SessionInformation,
    SessionRequest,
    VerifySession,
    read_monitor,
)


class DeclaredOptions:
    """The CLI adapter consumes FieldCodec's existing field plans."""
    help: ClassVar[str]

    @classmethod
    def add_parser[P: argparse.ArgumentParser](
            cls, subparsers: argparse._SubParsersAction[P]) -> None:
        cls.add_arguments(subparsers.add_parser(FieldCodec.encode(cls), help=cls.help))

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        FieldCodec.add_arguments(cls, parser)

    @classmethod
    def from_arguments(cls, args: argparse.Namespace) -> Self:
        return FieldCodec.decode_arguments(cls, args)


class BridgeStateTable(DurableTables):
    """The bridge's delivery.sqlite3: its binding and every delivery attempt.

    The bus holds the messages, not whether this harness was sent them, so
    nothing rebuilds these rows: a stored schema that differs is refused.
    """

    __slots__ = ()
    filename = 'delivery.sqlite3'


class BindingRequest(ABC):
    """What the command line asks of the state directory's one binding."""

    @abstractmethod
    def bind(self, db: CoordinationStore) -> Binding: ...


@dataclass(frozen=True)
class StoredBinding(BindingRequest):
    """No binding given: the directory's own, read without taking the writer."""

    def bind(self, db: CoordinationStore) -> Binding:
        rows = Binding.select(db)
        if len(rows) != 1:
            raise ValueError('Supply one complete Core/CLI binding for this state directory')
        return rows[0]


@dataclass(frozen=True)
class Binding(BridgeStateTable, TypedTable, BindingRequest):
    root: str
    bus: str = field(metadata={'wire_name': 'bus_thread'})
    thread: str = field(metadata={'wire_name': 'session'})

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        group = parser.add_argument_group('Core/CLI binding (all or none)')
        for _, key in FieldCodec.record_fields(cls):
            group.add_argument('--' + key.replace('_', '-'))

    @classmethod
    def from_arguments(cls, arguments: argparse.Namespace) -> BindingRequest:
        values = {key: vars(arguments)[key] for _, key in FieldCodec.record_fields(cls)
                  if vars(arguments)[key] is not None}
        return FieldCodec.decode(cls, {cls.family_discriminator: cls.declared_name, **values}) if values else StoredBinding()

    @classmethod
    @contextmanager
    def open(cls, state: Path):
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(CoordinationStore.writer(state / BridgeStateTable.filename)) as db:
            db.execute('PRAGMA synchronous=FULL')
            with SQLiteFile.write_transaction(db):
                if not BridgeStateTable.stored_schema(db):
                    BridgeStateTable.create_schema(db)
                BridgeStateTable.reconcile_schema(db)
            yield db

    def bind(self, db: CoordinationStore) -> Binding:
        """The first binding claims the directory; a later one must be the same."""
        with SQLiteFile.write_transaction(db):
            if not Binding.select(db):
                self.insert(db)
            if StoredBinding().bind(db) != self:
                raise ValueError('This state directory belongs to another Core/CLI conversation')
            return self


@dataclass
class Options:
    operation: Operation
    binding: BindingRequest
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
            bridge.log(f'Monitor parts mismatch; message remains pending: {delivery.id}')
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
class ObservedPart(BridgeStateTable, TypedTable):
    id: str = field(metadata={'sql': Column(primary_key=True)})
    number: int = field(metadata={'sql': Column(primary_key=True)})
    sender: str
    body: str
    record_id: str
    timestamp: float


@dataclass(frozen=True)
class DeliveryRow(BridgeStateTable, TypedTable):
    sequence_sql: ClassVar[str] = "json_extract({payload}, '$.seq')"
    indexes = (ExpressionIndex((sequence_sql,)),)

    @classmethod
    def resume_after(cls, db: CoordinationStore) -> MessageReference | None:
        """The existing journal owns attempted progress, including uncertain emissions."""
        latest = cls.read(db.execute(
            f'SELECT {cls._column_list(tuple(cls.column_projection()))} FROM "{cls.declared_name}" '
            f'ORDER BY {cls._sql(cls.sequence_sql)} DESC LIMIT 1'))
        return latest[0].message.reference if latest else None

    id: str = field(metadata={'sql': Column(primary_key=True)})
    payload: str
    outcome: type[Outcome] = field(metadata={'sql': Column(index=True)})
    monitor: MonitorEvidence = field(default_factory=UnrecordedMonitor)

    @cached_property
    def message(self) -> messages.Message:
        return messages.Message.from_wire(json.loads(self.payload))

    async def finish(self, bridge: CliBridge, outcome: type[Outcome]) -> None:
        outcome = self.outcome.followed_by(outcome)
        with bridge.db:
            replace(self, outcome=outcome).upsert(bridge.db)
        await outcome.acknowledge(bridge, self.message)

BridgeOptions = TypeVar('BridgeOptions', bound=Options)
OperationBridge = TypeVar('OperationBridge', bound='CliBridge')


@dataclass(frozen=True)
class Operation(DeclaredOptions, ABC, Generic[OperationBridge]):
    """A decoded CLI command owns its execution."""

    def binding(self, arguments: argparse.Namespace) -> BindingRequest:
        return Binding.from_arguments(arguments)

    @abstractmethod
    async def execute(self, bridge: OperationBridge) -> None: ...

    def transcript_source(self) -> Path:
        raise ValueError('This command does not own a live transcript subscription')

    def retired(self, bridge: OperationBridge) -> None:
        """Most operations have no runtime successor."""

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
class TargetField:
    """The declared field owns parsing, editor hints and requiredness."""
    plan: field_codec.FieldPlan
    value: object

    @classmethod
    def of(cls, plan: field_codec.FieldPlan, value: object) -> TargetField:
        editor = (CatalogTargetField if 'catalog' in plan.declared.metadata
                  else plan.setting('editor', cls))
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
        return FieldCodec.value_schema(self.annotation)

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

    async def prepare(self, catalogs: ConfigOptions,
                      selection: CatalogSelection) -> ChoiceTargetField:
        decoder = field_codec.compile_decoder(self.annotation)
        options = (SessionConfigSelectOption(
                       name=label, value=decoder.encode_editor(decoder.decode(value)))
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

    async def prepare(self, catalogs: ConfigOptions,
                      selection: CatalogSelection) -> CatalogTargetField:
        catalog = catalogs.catalog_for(self.catalog)
        options = await catalog.display_choices(selection)
        decoder = field_codec.compile_decoder(self.annotation)
        return replace(self, options=tuple(SessionConfigSelectOption(name=item.name,
            value=decoder.encode_editor(decoder.decode(item.value))) for item in options))

    def value_schema(self) -> dict[str, object]:
        return {**FieldCodec.value_schema(self.annotation),
                'configuration': FieldCodec.encode(self.catalog)}





class SendArguments(argparse.Namespace):
    command: str

class SendReport(field_codec.SubclassUnion, affix=""):
    def send(self, ctx: Comms, request: SendRequest) -> SendResult:
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
class SendRequest(DeclaredOptions):
    help = "Send to a thread (DM), #channel, or #all"
    sender: str = option("--from", default="")
    target: str = option("--to")
    body: str = option("--body")
    type: builtins.type[messages.MessageKind] = option("--type", default=messages.InfoKind,
        choices=messages.SendableMessageKind.names(), editor=ChoiceTargetField)
    role: builtins.type[messages.SendableRole] = option("--role", default=messages.ConversationRole)
    report: SendReport = option("--report", default=MissingReport(),
        help="Change report fields as a JSON object", type=FieldCodec.parse)


@dataclass(frozen=True, kw_only=True)
class Send(SendRequest, Operation[OperationBridge]):
    async def execute(self, bridge: OperationBridge) -> None:
        request = replace(self, sender=bridge.binding.bus)
        await bridge.show_sent(await bridge.comms.bus.when_open(
            partial(request.report.send, bridge.comms, request)))


class Inbox(Operation[OperationBridge]):
    @classmethod
    def serves(cls, bridge: type[CliBridge]) -> bool:
        """Delivery alone, without the transcript that confirms it on some bridges."""
        return not issubclass(bridge, TranscriptConfirmsDelivery)

    def start(self, bridge: OperationBridge, tasks: asyncio.TaskGroup) -> None:
        tasks.create_task(bridge.watch())

    async def execute(self, bridge: OperationBridge) -> None:
        await bridge.registered_watch(self)


class Watch(Inbox):
    @classmethod
    def serves(cls, bridge: type[CliBridge]) -> bool:
        return issubclass(bridge, PublishesTranscript)

    async def execute(self, bridge: OperationBridge) -> None:
        await bridge.watch_operation(self)

    def start(self, bridge: TranscriptBridge, tasks: asyncio.TaskGroup) -> None:
        super().start(bridge, tasks)
        tasks.create_task(bridge.publish_transcript())


@dataclass(frozen=True, kw_only=True)
class Serve(Watch):
    owner: ProcessIdentity = option('--owner', type=FieldCodec.parse)
    transcript: Annotated[Path, PathText] = option('--transcript')

    @classmethod
    def serves(cls, bridge: type[CliBridge]) -> bool:
        return issubclass(bridge, LineMonitorBridge)

    def transcript_source(self) -> Path:
        return self.transcript

    def command(self, record: WatchRecord, runtime: LaunchRuntime) -> tuple[str, ...]:
        return (
            str(runtime.interpreter.with_name(Path(record.command[0]).name)),
            type(self).__name__.lower(), *record.command[2:],
            '--owner', json.dumps(FieldCodec.encode_record(self.owner)),
            '--transcript', str(self.transcript),
        )

    @classmethod
    def launch(cls, claim: WatchClaim, owner: Callable[[], ProcessIdentity],
               transcript: Path) -> None:
        """The session hook supplies its identity; Serve owns launch and readiness."""
        claim.state().connect(
            lambda: cls(owner=owner(), transcript=transcript),
            lambda operation: VerifySession(owner=operation.owner),
        )

    def launch_record(self, claim: WatchClaim, record: WatchRecord,
                      request: SessionRequest) -> None:
        runtime = LaunchRuntime.installed()
        command = self.command(record, runtime)
        log = claim.path.with_suffix('.log')
        with log.open('ab') as output:
            child = SpawnedProcess.launch(
                command, env=runtime.environment(os.environ), output=output, runtime=runtime)
        deadline = time.monotonic() + 30
        while child.alive() and time.monotonic() < deadline:
            try:
                request.exchange(claim)
                return
            except (FileNotFoundError, ConnectionRefusedError):
                time.sleep(.05)
        raise RuntimeError(
            f'Session bridge startup not observed (PID {child.pid}); inspect {log}. '
            'No launch retry.'
        )

    @staticmethod
    def running(home: Path) -> tuple[SessionBridge, ...]:
        claims = set()
        for state in sorted(WatchClaim.directory(home).parent.glob('*-bridge-*')):
            journal = state / BridgeStateTable.filename
            if not journal.is_file():
                continue
            with closing(CoordinationStore.read_snapshot(journal)) as db:
                bindings = Binding.select(db)
            for binding in bindings:
                claim = WatchClaim(binding.thread, home)
                claims.update(claim.state().serving_claims())
        return tuple(SessionBridge(claim, InspectSession().information(claim))
                     for claim in sorted(claims, key=lambda claim: claim.thread))

    def retired(self, bridge: LineMonitorBridge) -> None:
        bridge.retirement.finish(self, bridge.watch_record())

    async def execute(self, bridge: LineMonitorBridge) -> None:
        if self.transcript.stem != bridge.binding.thread:
            raise ValueError('The live transcript belongs to another Claude session')
        self.owner.require_alive()
        await bridge.registered_watch(self)

    def start(self, bridge: LineMonitorBridge, tasks: asyncio.TaskGroup) -> None:
        super().start(bridge, tasks)
        tasks.create_task(MonitorService(bridge, self.owner, bridge.stop).run())


@dataclass(frozen=True)
class SessionBridge:
    """A live endpoint's own description, captured before install stops anything."""

    claim: WatchClaim
    observed: SessionInformation

    def pause(self, resources: ExitStack) -> tuple[PausedSessionBridge, ...]:
        if not self.observed.owner.alive():
            if self.observed.process.alive():
                self.observed.process.signal(signal.SIGTERM)
            print(f'Session bridge {self.claim.thread}: owner exited; left stopped', file=sys.stderr)
            return ()
        self.observed.owner.require_alive()
        peer = resources.enter_context(PauseSession(
            owner=self.observed.owner, process=self.observed.process).connection(self.claim))
        stream = resources.enter_context(peer.makefile('rb'))
        REPLIES.validate_json(stream.readline(4096)).require()
        return (PausedSessionBridge(self, peer),)


@dataclass(frozen=True)
class PausedSessionBridge:
    bridge: SessionBridge
    peer: socket.socket

    def resume(self) -> str:
        runtime = LaunchRuntime.installed()
        observed = self.bridge.observed
        name = self.bridge.claim.thread
        if not observed.owner.alive():
            print(f'Session bridge {name}: owner exited; left stopped', file=sys.stderr)
            return f'{name}: owner exited; left stopped'
        self.peer.sendall((json.dumps(FieldCodec.encode(runtime)) + '\n').encode())
        deadline = time.monotonic() + 30
        while observed.process.alive() and time.monotonic() < deadline:
            try:
                current = InspectSession().information(self.bridge.claim)
            except (FileNotFoundError, ConnectionRefusedError):
                time.sleep(.05)
                continue
            if current.process != observed.process or current.owner != observed.owner:
                raise ValueError(f'Session bridge {name}: process or owner changed')
            if current.runtime.prefix != runtime.prefix:
                raise ValueError(f'Session bridge {name}: wrong runtime {current.runtime.prefix}')
            print(f'Session bridge {name}: {observed.runtime.prefix} -> {current.runtime.prefix}',
                  file=sys.stderr)
            return name
        raise ValueError(f'Session bridge {name}: successor runtime was not observed')


class Enrol(Operation):
    @classmethod
    def serves(cls, bridge: type[CliBridge]) -> bool:
        return issubclass(bridge, LineMonitorBridge)

    async def execute(self, bridge: LineMonitorBridge) -> None:
        WatchClaim(bridge.binding.thread).enrol(bridge.watch_record())
        print('Session binding enrolled; SessionStart owns the bridge. Monitor runs watch.', flush=True)


@dataclass(frozen=True)
class IncomingFrame(ABC):
    message: messages.Message

    @property
    def text(self) -> str:
        origin = self.message.sender_role.origin_label
        return (f'Comms in · {self.message.sender} ({origin})\n'
                f'{self.message.body}\nMessage ID: {self.message.message_id}')

class BridgeLog(io.TextIOBase):
    """The bridge process's stderr, including tracebacks and partial print writes."""

    def __init__(self, stream: TextIO):
        self.stream = stream
        self.at_line_start = True

    def write(self, text: str) -> int:
        for line in text.splitlines(keepends=True):
            if self.at_line_start:
                self.stream.write(datetime.now().astimezone().isoformat(timespec='milliseconds') + ' ')
            self.stream.write(line)
            self.at_line_start = line.endswith('\n')
        return len(text)

    def flush(self) -> None:
        self.stream.flush()

    def fileno(self) -> int:
        return self.stream.fileno()


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
            Registration, RegistryFile.path_in(Path(self.binding.root)))
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

    @staticmethod
    def log(text: str) -> None:
        print(text, file=sys.stderr, flush=True)

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
        self.log(f'{result.declared_name}: {message.message_id} from {message.sender}')

    async def watch(self) -> None:
        await self.prepare_delivery()
        self.log(f'Watching {self.binding.bus} → {self.binding.thread}; unconfirmed attempts retained without replay')
        while True:
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
            outcome = await child.wait()
            if outcome.interrupted:
                self.log(f'Core watch PID {child.pid} {outcome.summary}; '
                         'reopening its subscription, retaining delivery attempts.')
                return
            await raise_child_exit(child, '')
        finally:
            await child.stop()

    async def run(self) -> None:
        current = asyncio.current_task()
        assert current is not None
        self.stop = current.cancel
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, self.stop)
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
            self.log(failure)
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


class MonitorIncomingFrame(IncomingFrame):
    @property
    @abstractmethod
    def parts(self) -> tuple[str, ...]: ...


class LineMonitorBridge(TranscriptConfirmsDelivery, TranscriptBridge[Options]):
    """Session-owned subscription and delivery journal, with replaceable readers."""

    incoming_frame: ClassVar[type[MonitorIncomingFrame]]
    window_bytes: ClassVar[int]

    def __init__(self, options: Options, binding: Binding, db: CoordinationStore) -> None:
        super().__init__(options, binding, db)
        self.reader: ReaderState = NoReader()
        self.reader_changed = asyncio.Condition()
        self.retirement: BridgeExit = BridgeExit()

    def watch_record(self) -> WatchRecord:
        command = (str(Path(sys.argv[0]).absolute()), Watch.__name__.lower(),
                   '--state', str(self.options.state.absolute()))
        return WatchRecord(self.binding.bus, self.binding.root, command)

    async def watch_operation(self, operation: Watch) -> None:
        await read_monitor(WatchClaim(self.binding.thread))

    async def accept(self, message: messages.Message) -> None:
        # Wait before recording an attempt. Detachment never creates
        # an attempt for a message that has not reached an attached reader.
        await self.prepare_delivery()
        try:
            await super().accept(message)
        except (BrokenPipeError, ConnectionResetError) as error:
            await self.report_failure(
                f'Monitor failed during {message.message_id}: {error}; '
                'the recorded attempt stays uncertain and will not be replayed.')

    async def prepare_delivery(self) -> None:
        await self.reader.prepare_delivery(self)

    async def confirm_probe(self, probe: MonitorProbe) -> None:
        await self.reader.confirm_probe(self, probe)

    async def part_observed(self, message_id: str, number: int) -> None:
        await self.reader.part_observed(message_id, number)

    @staticmethod
    def encode(text: str) -> str:
        # One reversible JSON string per line; no truncation or lost line breaks.
        return json.dumps(text, ensure_ascii=False)

    def new_attachment(self, peer: MonitorPeer, token: str) -> MonitorAttachment:
        return MonitorAttachment(peer, MonitorProbe(session=self.binding.thread, token=token))

    async def deliver(self, message: messages.Message) -> type[Outcome]:
        return await self.reader.deliver(self, message)

    async def deliver_to(self, attachment: MonitorAttachment, message: messages.Message) -> type[Outcome]:
        parts = self.incoming_frame(message).parts
        with self.db:
            DeliveryRow.update(self.db, where='id=?', parameters=(message.message_id,),
                               monitor=ReceivingMonitor(len(parts)))
        for number, part in enumerate(parts, 1):
            await attachment.emit(message.message_id, number, self.encode(part), self.window_bytes)
        return EmittedOutcome

    async def show_sent(self, result: SendResult) -> None:
        print(self.encode(self.sent_text(result)), flush=True)

    async def report_failure(self, text: str) -> None:
        self.log(text)


def run_bridge(bridge_type: type[CliBridge[BridgeOptions]], options: BridgeOptions) -> None:
    sys.stderr = BridgeLog(sys.stderr)
    os.umask(0o077)
    try:
        with Binding.open(options.state) as db, asyncio.Runner() as runner:
            bridge = bridge_type(options, options.binding.bind(db), db)
            with suppress(asyncio.CancelledError):
                runner.run(bridge.run())
        options.operation.retired(bridge)
    except* BlockingIOError:
        sys.exit('Another watcher owns this conversation')
