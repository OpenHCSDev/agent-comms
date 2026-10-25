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
import sqlite3
import sys
import time
import traceback
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager, ExitStack, closing, contextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import cached_property, partial
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar, Generic, Literal, Self, TextIO, TypeVar

from pydantic import Field, InstanceOf, PlainSerializer, PlainValidator, model_validator

from agent_comms import field_codec, messages
from agent_comms.agent_runtime import DeliversInput, ExternalRuntime, PublishesTranscript
from agent_comms.bus import Bus
from agent_comms.bus_tables import MessageIdentitySource
from agent_comms.change_report import NoChangeReport, ReportEvidence
from agent_comms.child_process import (
    AttachedChild,
    OutlivesLauncherLifetime,
    Platform,
    ProcessIdentity,
    SpawnedProcess,
    StreamingChildStdio,
)
from agent_comms.cli_protocol import (
    CliArguments,
    DeliveredOutcome,
    EmittedOutcome,
    FilteredOutcome,
    Outcome,
    TaggedWire,
    UnconfirmedOutcome,
    WatchLine,
    raise_child_exit,
)
from agent_comms.comms import Comms, wire
from agent_comms.config_options import (
    CatalogChoice,
    CatalogConfigOption,
    CatalogSelection,
    ConfigOptions,
)
from agent_comms.coordination_errors import CoordinationError
from agent_comms.declared_family import DeclaredFamily
from agent_comms.declared_verb import DeclaredOptions
from agent_comms.field_codec import FieldCodec, MessageId, PathText, SubclassUnion
from agent_comms.installer.refusals import UpgradeRefused
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.message_reference import (
    BridgeBeginning,
    BridgePosition,
    BridgeStart,
    InboxStart,
    MessageReference,
)
from agent_comms.read_basis import ExplicitWatchAcknowledgment
from agent_comms.registration import Registration
from agent_comms.runtime_updates import ReplyingThreadOutput, RuntimeUpdate, ThreadOutput
from agent_comms.send_result import SendResult
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.thread_status import ThreadStatus
from agent_comms.thread_text import AbsentText, GivenText, TextChoice
from agent_comms.threads import Thread
from agent_comms.typed_table import (
    Column,
    DurableTables,
    ExpressionIndex,
    TypedRow,
    TypedTable,
    WriterOwnedFile,
)
from agent_comms.wire_models import WireModel
from agent_comms_hooks.watch import HeldClaim, NoSessionBridge, WatchClaim, WatchRecord

from ..locked_store import RegistryFile, SQLiteFile
from .monitor import (
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

if TYPE_CHECKING:
    from agent_comms.task_attachments.commander_pings import CommanderUpdate


class BridgeStateTable(DurableTables):
    """The bridge's delivery.sqlite3: its binding and every delivery attempt.

    The bus holds the messages, not whether this harness was sent them, so
    nothing rebuilds these rows: a stored schema that differs is carried onto the
    declaration where it says how, keeping every row, and refused where it cannot.
    """

    __slots__ = ()
    filename = WatchRecord.store_name


class BindingRequest(ABC):
    """What the command line asks of the state directory's one binding."""

    @abstractmethod
    def bind(self, db: sqlite3.Connection) -> BindingAddress: ...


class BindingSession(ABC):
    """The command line or the harness owns this session's identity."""

    @classmethod
    @abstractmethod
    def add_arguments(cls, parser: argparse._ActionsContainer) -> None: ...

    @abstractmethod
    def resolve(self, bridge: type[CliBridge]) -> str: ...

    @abstractmethod
    def supplied(self) -> dict[str, str]: ...


@dataclass(frozen=True)
class SuppliedSession(BindingSession):
    session: str

    @classmethod
    def add_arguments(cls, parser: argparse._ActionsContainer) -> None:
        parser.add_argument('--session', dest='binding_session', type=cls, required=True)

    def resolve(self, bridge: type[CliBridge]) -> str:
        return self.session

    def supplied(self) -> dict[str, str]:
        return {FieldCodec.record_key(BindingIdentity, 'thread'): self.session}


@dataclass(frozen=True)
class EnvironmentSession(BindingSession):
    @classmethod
    def add_arguments(cls, parser: argparse._ActionsContainer) -> None:
        parser.add_argument(
            '--session', dest='binding_session', type=SuppliedSession, default=cls())

    def resolve(self, bridge: type[CliBridge]) -> str:
        return bridge.environment_session()

    def supplied(self) -> dict[str, str]:
        return {}


@dataclass(frozen=True)
class StoredBinding(BindingRequest):
    """No binding given: the directory's own, read without taking the writer."""

    def bind(self, db: sqlite3.Connection) -> BindingAddress:
        return Binding.require(db).address()


@dataclass(frozen=True)
class GivenBinding(BindingRequest):
    """Some binding fields given: each overrides the stored binding's field, and a field
    missing from both is refused by name when the merged fields decode."""

    given: dict[str, str]

    @classmethod
    def from_arguments(cls, arguments: CliArguments, bridge: type[CliBridge]) -> Self:
        keys = {key for _, key in FieldCodec.record_fields(BindingAddress)}
        values = {key: value for key, value in vars(arguments).items() if key in keys}
        arguments.base_state = arguments.state
        arguments.state = arguments.state / arguments.binding_thread.thread
        return cls({Binding.family_discriminator: Binding.declared_name,
                    **values, **arguments.binding_session.supplied(),
                    FieldCodec.record_key(BindingAddress, 'bus'): arguments.binding_thread.thread})

    def bind(self, db: sqlite3.Connection) -> BindingAddress:
        stored = Binding.select(db)
        if stored:
            supplied = {name: value for plan in FieldCodec.field_plans(BindingAddress)
                        for name, value in plan.decoded(self.given).items()}
            return replace(stored[0].address(), **supplied)
        root = self.given[FieldCodec.record_key(BindingAddress, 'root')]
        return FieldCodec.decode(BindingAddress, {
            'start': FieldCodec.encode(Binding.bus_head(root)),
            **{key: value for key, value in self.given.items()
               if key != Binding.family_discriminator},
        })


class ResolvedBinding(GivenBinding):
    """A bare command resolves its reader from the harness session's enrolment."""

    @classmethod
    def from_arguments(cls, arguments: CliArguments, bridge: type[CliBridge]) -> Self:
        session = arguments.binding_session.resolve(bridge)
        bindings = WatchClaim(session).state().bindings()
        if len(bindings) != 1:
            raise ValueError(
                f'Expected one enrolled binding for session {session}; '
                f'found {[record.bus for record in bindings]}'
            )
        record, = bindings
        arguments.root = record.root
        arguments.binding_thread = GivenThread(record.bus)
        arguments.state = record.base_state
        arguments.binding_session = SuppliedSession(session)
        return super().from_arguments(arguments, bridge)


class BindingThread(ABC):
    """The bus flag's binding request and its resolved thread answer."""

    @property
    @abstractmethod
    def request(self) -> type[GivenBinding]: ...

    @property
    @abstractmethod
    def thread(self) -> str: ...


@dataclass(frozen=True, slots=True)
class GivenThread(BindingThread):
    """An explicit bus flag selects the explicit binding request."""

    thread: str = field()
    request = GivenBinding


@dataclass(frozen=True, slots=True)
class ResolvedThread(BindingThread):
    """Without a bus flag, the operation owns how its reader is resolved."""

    request: type[GivenBinding] = field()

    @property
    def thread(self) -> str:
        raise ValueError('Resolve the enrolled binding before reading its thread')


@dataclass(frozen=True, kw_only=True)
class SessionBinding(ABC):
    """The session and start shared by a requested address and a registered reader."""

    root: str
    thread: Annotated[str, field_codec.WireName("session")]
    start: BridgePosition

    @property
    @abstractmethod
    def bus(self) -> str: ...

    @abstractmethod
    def recipient(self, comms: Comms) -> ThreadIncarnation: ...

    def registered(self, comms: Comms) -> Binding:
        return Binding(root=self.root, owner=self.recipient(comms),
                       thread=self.thread, start=self.start)


@dataclass(frozen=True, kw_only=True, slots=True)
class BindingAddress(SessionBinding):
    """A command-line address, resolved only after the operation registers its thread."""

    bus: Annotated[str, field_codec.WireName("bus_thread")]

    def recipient(self, comms: Comms) -> ThreadIncarnation:
        return comms.registry.require(self.bus).incarnation


@dataclass(frozen=True, kw_only=True)
class BindingIdentity(SessionBinding):
    owner: ThreadIncarnation

    @property
    def bus(self) -> str:
        return self.owner.name

    def address(self) -> BindingAddress:
        return BindingAddress(root=self.root, bus=self.bus, thread=self.thread, start=self.start)

    def recipient(self, comms: Comms) -> ThreadIncarnation:
        current = comms.registry.require_lookup(self.owner.require_recorded().lookup)
        return current.incarnation

@dataclass(frozen=True)
class Binding(BindingIdentity, BridgeStateTable, TypedTable):

    @staticmethod
    def bus_head(root: str) -> BridgePosition:
        bus = Bus(Bus.path_in(Path(root)))
        if not bus.path.is_file():
            raise FileNotFoundError(bus.path)
        with bus.observing() as db:
            latest = MessageIdentitySource.select(
                db, order_by=('seq',), descending=True, limit=1)
            return (BridgeStart(MessageReference(latest[0].seq, latest[0].message_id))
                    if latest else BridgeBeginning())

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser,
                      session: type[BindingSession], request: type[GivenBinding]) -> None:
        group = parser.add_argument_group(
            'Core/CLI binding (each given field overrides the stored binding)')
        group.add_argument('--root', default=argparse.SUPPRESS)
        group.add_argument('--bus-thread', dest='binding_thread', type=GivenThread,
                           default=ResolvedThread(request))
        session.add_arguments(group)

    @classmethod
    @contextmanager
    def open(cls, state: Path):
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(SQLiteFile.writer(state / BridgeStateTable.filename)) as db:
            db.execute('PRAGMA synchronous=FULL')
            BridgeStateTable.install_schema(db, WriterOwnedFile())
            yield db

    def bind(self, db: sqlite3.Connection) -> Binding:
        """Rebind the reader's transport without replacing its delivery progress."""
        with SQLiteFile.write_transaction(db):
            if not Binding.select(db):
                self.insert(db)
            stored = Binding.require(db)
            if stored.root != self.root or stored.owner != self.owner:
                raise ValueError('This state directory belongs to another Core/CLI conversation')
            Binding.update(db, where='1', thread=self.thread)
            return replace(stored, thread=self.thread)


@dataclass
class Options:
    operation: Operation
    binding: BindingRequest
    state: Path
    base_state: Path

    @classmethod
    def parse(cls, description: str | None, state: Path, bridge_type: type[CliBridge]) -> Self:
        common = argparse.ArgumentParser(add_help=False)
        common.add_argument('--state', type=Path, default=state)
        bridge_type.add_arguments(common)
        parser = argparse.ArgumentParser(description=description)
        commands = parser.add_subparsers(required=True)
        for operation in Operation.offered(bridge_type):
            command = commands.add_parser(
                operation.__name__.lower(), parents=[common],
                help=operation.help, description=operation.help)
            Binding.add_arguments(command, operation.binding_session, operation.binding_request)
            operation.add_arguments(command)
            command.set_defaults(operation=operation)
        arguments = parser.parse_args(namespace=CliArguments())
        arguments.operation = arguments.operation.from_arguments(arguments)
        arguments.binding = arguments.binding_thread.request.from_arguments(arguments, bridge_type)
        return cls(**{declared.name: vars(arguments)[key]
                      for declared, key in FieldCodec.record_fields(cls)})


@dataclass(frozen=True)
class MonitorEvidence(DeclaredFamily):
    """Unsent and terminal observations cannot accept another delivery."""

    async def confirm(self, bridge: LineMonitorBridge, delivery: DeliveryRow,
                      notification: MonitorPart, record: ObservedMonitor) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class UnrecordedMonitor(MonitorEvidence):
    pass


@dataclass(frozen=True)
class ReceivingMonitor(MonitorEvidence):
    total: int

    async def confirm(self, bridge: LineMonitorBridge, delivery: DeliveryRow,
                      notification: MonitorPart, record: ObservedMonitor) -> tuple[str, ...]:
        if notification.total != self.total:
            return ()
        part = ObservedPart(delivery.id, notification.number, notification.sender,
                            notification.body, record.record_id, record.timestamp)
        with bridge.db:
            part.insert_if_absent(bridge.db)
        await bridge.part_observed(delivery.id, notification.number)
        if ObservedPart.count(bridge.db, id=delivery.id) != self.total:
            return ()
        parts = ObservedPart.select(bridge.db, id=delivery.id, order_by=('number',))
        message = delivery.message
        if (''.join(part.body for part in parts) != message.body
                or any(part.sender != message.sender for part in parts)):
            with bridge.db:
                replace(delivery, monitor=MismatchedMonitor('Observed parts do not equal the original message')).upsert(bridge.db)
            bridge.log(f'Monitor parts mismatch; message remains pending: {delivery.id}')
            return ()
        await replace(delivery, monitor=record).finish(bridge, DeliveredOutcome)
        return (message.exact_reply_target,)


@dataclass(frozen=True)
class MismatchedMonitor(MonitorEvidence):
    reason: str


@dataclass(frozen=True)
class ObservedMonitor(MonitorEvidence):
    record_id: str
    timestamp: float


@dataclass(frozen=True)
class ObservedPart(BridgeStateTable, TypedTable):
    id: Annotated[MessageId, Column(primary_key=True)]
    number: Annotated[int, Column(primary_key=True)]
    sender: str
    body: str
    record_id: str
    timestamp: float


@dataclass(frozen=True)
class JournalMessage(TypedRow):
    """Journal identity and payload are enough to recover a delivery position."""

    id: Annotated[MessageId, Column(primary_key=True)]
    payload: str

    @cached_property
    def message(self) -> messages.Message:
        return messages.Message.from_wire(json.loads(self.payload))


@dataclass(frozen=True)
class DeliveryRow(JournalMessage, BridgeStateTable, TypedTable):
    sequence_sql: ClassVar[str] = "json_extract({payload}, '$.seq')"
    indexes = (ExpressionIndex((sequence_sql,)),)

    @classmethod
    def latest_reference(cls, db: sqlite3.Connection) -> tuple[MessageReference, ...]:
        """The sequence index selects the newest attempt, including uncertain emissions;
        the cursor is its seq and id, so a payload in an older wire shape never decodes."""
        sequence = cls._sql(cls.sequence_sql)
        return tuple(
            MessageReference(seq, FieldCodec.decode(MessageId, message_id))
            for seq, message_id in db.execute(
                f'SELECT {sequence}, {cls._sql("{id}")} FROM "{cls.declared_name}" '
                f'ORDER BY {sequence} DESC LIMIT 1'))

    @classmethod
    def resume_after(cls, db: sqlite3.Connection, binding: SessionBinding) -> InboxStart:
        """The binding and journal own progress, including uncertain emissions."""
        return max((binding.start, *(BridgeStart(reference)
                                     for reference in cls.latest_reference(db))),
                   key=lambda point: point.cursor())

    outcome: Annotated[type[Outcome], Column(index=True)]
    monitor: MonitorEvidence = field(default_factory=UnrecordedMonitor)

    async def finish(self, bridge: CliBridge, outcome: type[Outcome]) -> None:
        await self.outcome.finish(bridge, self, outcome)


BridgeOptions = TypeVar("BridgeOptions", bound=Options)
OperationBridge = TypeVar("OperationBridge", bound="CliBridge")


@dataclass(frozen=True)
class Operation(DeclaredOptions, ABC, Generic[OperationBridge]):
    """A decoded CLI command owns its execution."""

    help: ClassVar[str]
    """The one line `--help` shows; every offered command declares it."""

    binding_session: ClassVar[type[BindingSession]] = EnvironmentSession
    binding_request: ClassVar[type[GivenBinding]] = ResolvedBinding

    @abstractmethod
    async def execute(self, bridge: OperationBridge) -> None: ...

    async def register(self, bridge: CliBridge) -> None:
        """Most operations require an already registered reader."""

    async def prepare(self, bridge: CliBridge) -> None:
        await self.register(bridge)
        bridge.binding = bridge.address.registered(bridge.comms).bind(bridge.db)

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
        editor = (
            CatalogTargetField
            if field_codec.Catalog.single(plan.annotation)
            else plan.setting(field_codec.Editor, cls)
        )
        return editor(plan, value)

    @property
    def annotation(self) -> object:
        return self.plan.annotation

    @property
    def name(self) -> str:
        return self.plan.key

    @property
    def multiline(self) -> bool:
        return self.plan.setting(field_codec.Multiline, False)

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
    options: tuple[CatalogChoice, ...] = ()

    @property
    def editor_declarations(self) -> tuple[object, ...]:
        return ()

    @property
    def choices(self) -> tuple[tuple[str, str], ...]:
        return tuple((choice.name, choice.editor_value) for choice in self.options)

    async def prepare(self, catalogs: ConfigOptions,
                      selection: CatalogSelection) -> ChoiceTargetField:
        decoder = field_codec.compile_decoder(self.annotation)
        options = (CatalogChoice(
                       name=label, value=decoder.encode_editor(decoder.decode(value)))
                   for label, value in decoder.editor_choices())
        return replace(self, options=tuple(options))

    def editor_hints(self, choices: Callable[[ChoiceTargetField], object]) -> tuple[object, ...]:
        return (choices(self),)


class CatalogTargetField(ChoiceTargetField):
    @property
    def catalog(self) -> type[CatalogConfigOption]:
        return self.plan.setting(field_codec.Catalog, CatalogConfigOption)

    @property
    def editor_declarations(self) -> tuple[object, ...]:
        return (self.catalog,)

    async def prepare(self, catalogs: ConfigOptions,
                      selection: CatalogSelection) -> CatalogTargetField:
        catalog = catalogs.catalog_for(self.catalog)
        options = await catalog.display_choices(selection)
        decoder = field_codec.compile_decoder(self.annotation)
        return replace(self, options=tuple(CatalogChoice(name=item.name,
            value=decoder.encode_editor(decoder.decode(item.value))) for item in options))

    def value_schema(self) -> dict[str, object]:
        return {**FieldCodec.value_schema(self.annotation),
                'configuration': FieldCodec.encode(self.catalog)}


@dataclass(frozen=True, kw_only=True)
class SendRequest(DeclaredOptions):
    help = "Send to a thread (DM), #channel, or #all"
    sender: Annotated[str, field_codec.Argument(flags=("--from",))] = ""
    target: Annotated[str, field_codec.Argument(flags=("--to",))]
    body: Annotated[str, field_codec.Argument(flags=("--body",))]
    type: Annotated[
        builtins.type[messages.MessageKind],
        field_codec.Editor(ChoiceTargetField),
        field_codec.Argument(
            flags=("--type",), options={"choices": messages.SendableMessageKind.names()}
        ),
    ] = messages.InfoKind
    role: Annotated[
        builtins.type[messages.SendableRole], field_codec.Argument(flags=("--role",))
    ] = messages.ConversationRole
    report: Annotated[
        ReportEvidence,
        field_codec.Argument(
            flags=("--report",),
            options={"help": "Change report fields as a JSON object", "type": FieldCodec.parse},
        ),
    ] = NoChangeReport()

    def sent(self, comms: Comms) -> SendResult:
        return comms.messaging.send(self.sender, self.target, self.body, self.type,
                                    role=self.role(), report=self.report)


@dataclass(frozen=True, kw_only=True)
class Send(SendRequest, Operation[OperationBridge]):
    async def execute(self, bridge: OperationBridge) -> None:
        request = replace(self, sender=bridge.binding.bus)
        await bridge.show_sent(await bridge.comms.bus.when_open(
            partial(request.sent, bridge.comms)))


class Inbox(Operation[OperationBridge]):
    help = "Deliver this binding's bus messages to its harness until stopped"
    @classmethod
    def serves(cls, bridge: type[CliBridge]) -> bool:
        """Delivery alone, without the transcript that confirms it on some bridges."""
        return not issubclass(bridge, TranscriptConfirmsDelivery)

    async def start(self, bridge: OperationBridge, tasks: asyncio.TaskGroup,
                    endpoint: ExitStack) -> None:
        registry = await asyncio.to_thread(
            Registration, RegistryFile.path_in(Path(bridge.binding.root)))
        await bridge.comms.bus.when_open(lambda: endpoint.enter_context(
            registry.external_process(bridge.binding.bus, bridge.session_process())))
        tasks.create_task(bridge.watch())

    def claimed(self, claim: WatchClaim,
                record: WatchRecord) -> AbstractContextManager[HeldClaim]:
        return claim.held(record)

    def keep(self, bridge: OperationBridge, held: HeldClaim) -> None:
        """Only a session-owned serve has a successor to keep its claim for."""

    async def execute(self, bridge: OperationBridge) -> None:
        await bridge.registered_watch(self)


class BindingCommander(SubclassUnion, TextChoice, affix="BindingCommander"):
    """Who commands a newly bound session, or an independent user's session."""

    @abstractmethod
    async def register(self, bridge: CliBridge) -> None: ...


@dataclass(frozen=True, slots=True)
class CommandedBy(GivenText, BindingCommander):
    text: str

    async def register(self, bridge: CliBridge) -> None:
        worktree = str(await bridge.session_worktree())
        await bridge.comms.bus.when_open(partial(
            bridge.comms.registry.register_commanded_unit,
            bridge.address.bus, self.text, worktree, bridge.session_process(),
        ))


@dataclass(frozen=True, slots=True)
class Uncommanded(AbsentText, BindingCommander):
    """A user's own session has no commanded audience or fork parent."""

    async def register(self, bridge: CliBridge) -> None:
        thread = Thread(
            bridge.address.bus, frozenset(), str(await bridge.session_worktree()),
            process_identity=bridge.session_process(), execution=ExternalThreadExecution,
        )
        await bridge.comms.bus.when_open(partial(bridge.comms.registry.declare, thread))


@dataclass(frozen=True)
class Watch(Inbox):
    help = "Deliver this binding's bus messages and publish its harness transcript to the bus"

    commander: Annotated[
        BindingCommander, field_codec.Argument(flags=("--commander",))
    ] = Uncommanded()

    @classmethod
    def serves(cls, bridge: type[CliBridge]) -> bool:
        return issubclass(bridge, PublishesTranscript)

    async def execute(self, bridge: OperationBridge) -> None:
        await bridge.watch_operation(self)

    async def register(self, bridge: CliBridge) -> None:
        if not bridge.comms.registry.snapshot().taken(bridge.address.bus):
            await self.commander.register(bridge)

    async def start(self, bridge: TranscriptBridge, tasks: asyncio.TaskGroup,
                    endpoint: ExitStack) -> None:
        await super().start(bridge, tasks, endpoint)
        tasks.create_task(bridge.publish_transcript())


@dataclass(frozen=True, kw_only=True)
class Serve(Watch):
    help = "The session bridge process that connect and the SessionStart hook launch; not run by hand"
    owner: Annotated[
        ProcessIdentity,
        field_codec.Argument(flags=("--owner",), options={"type": FieldCodec.parse}),
    ]
    transcript: Annotated[Path, PathText, field_codec.Argument(flags=("--transcript",))]

    @classmethod
    def serves(cls, bridge: type[CliBridge]) -> bool:
        return issubclass(bridge, LineMonitorBridge)

    def transcript_source(self) -> Path:
        return self.transcript

    def command(self, record: WatchRecord, runtime: LaunchRuntime) -> tuple[str, ...]:
        return (
            str(runtime.interpreter.with_name(Path(record.command[0]).name)),
            type(self).__name__.lower(), *record.command[2:], *self.arguments(),
        )

    def arguments(self) -> tuple[str, ...]:
        return ('--owner', json.dumps(FieldCodec.encode_record(self.owner)),
                '--transcript', str(self.transcript))

    def successor(self, claim: int) -> ResumedServe:
        """The same owner's serve, holding this one's claim across exec."""
        return ResumedServe(owner=self.owner, transcript=self.transcript, claim=claim)

    def keep(self, bridge: LineMonitorBridge, held: HeldClaim) -> None:
        bridge.retirement = bridge.retirement.keeping(held)

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
        offset = log.stat().st_size if log.exists() else 0
        with log.open('ab') as output:
            child = SpawnedProcess.launch(
                command, env=runtime.environment(os.environ), output=output, runtime=runtime,
                lifetime=OutlivesLauncherLifetime)
        announced = False
        while child.alive():
            try:
                request.exchange(claim.socket_path)
                return
            except (FileNotFoundError, ConnectionRefusedError):
                if not announced:
                    print(
                        f'Waiting for session bridge PID {child.pid} to answer; log {log}',
                        file=sys.stderr, flush=True,
                    )
                    announced = True
                time.sleep(.05)
        summary = child.reap().summary
        with log.open('rb') as recorded:
            recorded.seek(offset)
            appended = recorded.read().decode(errors='replace').strip()
        raise RuntimeError(
            f'Session bridge PID {child.pid} exited before answering ({summary}): {appended}'
        )

    @staticmethod
    def running(home: Path) -> tuple[SessionBridge, ...]:
        bridges = []
        for endpoint in WatchClaim.endpoints(home):
            try:
                observed = InspectSession().information(endpoint)
            except (FileNotFoundError, ConnectionRefusedError):
                # A bridge may exit after enumeration, leaving its old socket behind.
                continue
            claim = WatchClaim(observed.transcript.stem, home)
            if claim.socket_path != endpoint:
                raise ValueError('Session endpoint reported a different claim identity')
            bridges.append(SessionBridge(claim, observed))
        return tuple(sorted(bridges, key=lambda bridge: bridge.claim.thread))

    def retired(self, bridge: LineMonitorBridge) -> None:
        bridge.retirement.finish(self, bridge.watch_record(bridge.binding))

    async def execute(self, bridge: LineMonitorBridge) -> None:
        if self.transcript.stem != bridge.binding.thread:
            raise ValueError('The live transcript belongs to another Claude session')
        self.owner.require_alive()
        await bridge.registered_watch(self)

    async def start(self, bridge: LineMonitorBridge, tasks: asyncio.TaskGroup,
                    endpoint: ExitStack) -> None:
        await super().start(bridge, tasks, endpoint)
        tasks.create_task(MonitorService(bridge, self.owner, bridge.stop).run())


@dataclass(frozen=True, kw_only=True)
class ResumedServe(Serve):
    """A serve exec'd in place by an install: it adopts the claim its predecessor kept."""

    help = "A session bridge an install resumes in place, adopting its predecessor's claim"

    claim: Annotated[int, field_codec.Argument(flags=("--claim",), options={"type": int})]

    def arguments(self) -> tuple[str, ...]:
        return (*super().arguments(), '--claim', str(self.claim))

    def claimed(self, claim: WatchClaim,
                record: WatchRecord) -> AbstractContextManager[HeldClaim]:
        return claim.adopted(self.claim, record)


class ConfirmableBridge(ABC):
    @abstractmethod
    def confirm(self) -> BridgeResume: ...


class ResumableBridge(ABC):
    @abstractmethod
    def release(self) -> ConfirmableBridge: ...

    def require_paused(self) -> None:  # noqa: B027 - default hook
        """A pause the bridge acknowledged, or one with nothing left to pause, refuses nothing."""


@dataclass(frozen=True)
class BridgeResume(ResumableBridge, ConfirmableBridge, DeclaredFamily, affix="Bridge"):
    name: str

    def release(self) -> Self:
        return self

    def confirm(self) -> Self:
        return self

    def failures(self) -> tuple[UnresumedBridge, ...]:
        """A bridge that came back, or whose owner left, fails nothing."""
        return ()

    @abstractmethod
    def lines(self) -> tuple[str, ...]: ...


class UnresumedBridge(BridgeResume, ValueError):
    """A bridge that should have come back on the new runtime and did not."""

    def failures(self) -> tuple[UnresumedBridge, ...]:
        return (self,)

    def __str__(self) -> str:
        return "\n".join(self.lines())


@dataclass(frozen=True)
class ResumedBridge(BridgeResume):
    from_runtime: LaunchRuntime
    to_runtime: LaunchRuntime

    def lines(self) -> tuple[str, ...]:
        return (f"Session bridge {self.name}: {self.from_runtime.prefix} -> "
                f"{self.to_runtime.prefix}",)


@dataclass(frozen=True)
class UnacknowledgedPause(UnresumedBridge):
    """A bridge that never answered pause may still hold bus resources: install refuses."""

    socket: Path
    timeout: float
    paused: PausedSessionBridge = field(compare=False, repr=False)

    def require_paused(self) -> None:
        raise UpgradeRefused(str(self))

    def release(self) -> Self:
        """The bridge may still be closing its bus resources; its runtime waits in the socket
        for the ready it writes once they close, so it resumes instead of staying stopped.
        The refusal stands."""
        self.paused.release()
        return self

    def lines(self) -> tuple[str, ...]:
        return (f"Session bridge {self.name} at {self.socket} did not acknowledge pause "
                f"within {self.timeout:g} s",)


class StoppedBridge(BridgeResume):
    """Nothing is left serving the claim, which is all a pause asks: no failure."""


@dataclass(frozen=True)
class OwnerExitedBridge(StoppedBridge):
    def lines(self) -> tuple[str, ...]:
        return (f"Session bridge {self.name}: owner exited; left stopped",)


@dataclass(frozen=True)
class ExitedBridge(StoppedBridge):
    """Its socket closed unanswered and its own process has exited."""

    def lines(self) -> tuple[str, ...]:
        return (f"Session bridge {self.name}: bridge exited during pause; left stopped",)


@dataclass(frozen=True)
class ReplacedBridge(UnresumedBridge):
    observed: ProcessIdentity
    current: ProcessIdentity

    def lines(self) -> tuple[str, ...]:
        return (f"Session bridge {self.name}: process or owner changed "
                f"({self.observed} -> {self.current}); re-arm this bridge",)


@dataclass(frozen=True)
class UnobservedBridge(UnresumedBridge):
    reason: str

    def lines(self) -> tuple[str, ...]:
        return (f"Session bridge {self.name}: new runtime was not observed "
                f"({self.reason}); re-arm this bridge",)


@dataclass(frozen=True)
class SessionBridge:
    """A live endpoint's own description, captured before install stops anything."""

    claim: WatchClaim
    observed: SessionInformation

    def pause(self, resources: ExitStack) -> ResumableBridge:
        if not self.observed.owner.alive():
            if self.observed.process.alive():
                self.observed.process.signal(signal.SIGTERM)
            return OwnerExitedBridge(self.claim.thread)
        self.observed.owner.require_alive()
        request = PauseSession(owner=self.observed.owner, process=self.observed.process)
        peer = resources.enter_context(request.connection(self.claim.socket_path))
        stream = resources.enter_context(peer.makefile('rb'))
        paused = PausedSessionBridge(self, peer)
        try:
            request.read_reply(stream)
        except TimeoutError:
            return UnacknowledgedPause(self.claim.thread, self.claim.socket_path,
                                       request.reply_timeout, paused)
        except ConnectionResetError:
            return self.closed(paused, request.reply_timeout)
        return paused

    def closed(self, paused: PausedSessionBridge, timeout: float) -> ResumableBridge:
        """A socket closed unanswered: stopped once its own process has exited, otherwise the
        live process may still hold bus resources and the pause stays unacknowledged."""
        Platform.current().wait_any_exited((self.observed.process,), timeout)
        if self.observed.process.alive():
            return UnacknowledgedPause(self.claim.thread, self.claim.socket_path, timeout, paused)
        return ExitedBridge(self.claim.thread)


@dataclass
class SessionBridgeResumes:
    """Every bridge an install paused. One failed resume cancels neither another resume nor
    the install steps after it: each outcome is reported, then the install exits non-zero."""

    paused: deque[ResumableBridge] = field(default_factory=deque)
    released: deque[ConfirmableBridge] = field(default_factory=deque)
    outcomes: list[BridgeResume] = field(default_factory=list)

    @contextmanager
    def holding(self, bridges: Iterable[SessionBridge]) -> Iterator[None]:
        """Each paused peer keeps its socket until released. Every exit releases and confirms
        what is still paused, so no bridge waits on a runtime that never comes."""
        with ExitStack() as resources:
            try:
                for bridge in bridges:
                    self.paused.append(bridge.pause(resources))
                    self.paused[-1].require_paused()
                yield
            finally:
                self.release()
                self.confirm()

    def release(self) -> None:
        """Give every paused peer its selected runtime before waiting for any successor."""
        while self.paused:
            self.released.append(self.paused[0].release())
            self.paused.popleft()

    def confirm(self) -> None:
        """Consume each released peer once, retaining outcomes for the terminal report."""
        while self.released:
            self.outcomes.append(self.released[0].confirm())
            self.released.popleft()

    @property
    def failures(self) -> tuple[UnresumedBridge, ...]:
        return tuple(error for outcome in self.outcomes for error in outcome.failures())

    def lines(self) -> tuple[str, ...]:
        return tuple(line for outcome in self.outcomes for line in outcome.lines())

    def require_resumed(self) -> None:
        failures = self.failures
        if failures:
            raise ExceptionGroup('Session bridges not resumed', failures)


@dataclass(frozen=True)
class PausedSessionBridge(ResumableBridge):
    bridge: SessionBridge
    peer: socket.socket

    def release(self) -> ConfirmableBridge:
        runtime = LaunchRuntime.installed()
        observed = self.bridge.observed
        name = self.bridge.claim.thread
        if not observed.owner.alive():
            return OwnerExitedBridge(name)
        try:
            self.peer.sendall((json.dumps(FieldCodec.encode(runtime)) + '\n').encode())
        except OSError as error:
            return UnobservedBridge(name, f"{type(error).__name__}: {error}")
        return ReleasedSessionBridge(self.bridge, runtime)


@dataclass(frozen=True)
class ReleasedSessionBridge(ConfirmableBridge):
    """A peer already given its runtime; its successor can start while install continues."""

    bridge: SessionBridge
    runtime: LaunchRuntime

    def confirm(self) -> BridgeResume:
        """The claim says whether the successor is still starting: its lock outlives the exec
        and is released only when the process exits, so no deadline guesses it."""
        observed = self.bridge.observed
        claim = self.bridge.claim
        runtime = self.runtime
        try:
            current = InspectSession().awaited(claim, self.announce)
        except NoSessionBridge:
            return UnobservedBridge(
                claim.thread, f"process exited before serving {runtime.prefix}")
        except OSError as error:
            return UnobservedBridge(claim.thread, f"{type(error).__name__}: {error}")
        if current.process != observed.process or current.owner != observed.owner:
            return ReplacedBridge(claim.thread, observed.process, current.process)
        if current.runtime.prefix != runtime.prefix:
            return UnobservedBridge(
                claim.thread, f"observed {current.runtime.prefix}, expected {runtime.prefix}")
        return ResumedBridge(claim.thread, observed.runtime, current.runtime)

    def announce(self, claim: WatchClaim) -> None:
        print(f"Waiting for session bridge {claim.thread} to serve {self.runtime.prefix}",
              file=sys.stderr, flush=True)


class Enrol(Operation):
    help = "Record this session's binding without starting its bridge; the next SessionStart starts it"
    reported: ClassVar[str] = (
        'Session binding enrolled; the next SessionStart starts the bridge. Monitor runs watch.')

    @classmethod
    def serves(cls, bridge: type[CliBridge]) -> bool:
        return issubclass(bridge, LineMonitorBridge)

    async def prepare(self, bridge: CliBridge) -> None:
        """Enrolment leaves registration and the durable binding to SessionStart."""

    async def execute(self, bridge: LineMonitorBridge) -> None:
        WatchClaim(bridge.address.thread).enrol(bridge.watch_record(bridge.address))
        self.begin(bridge)
        print(self.reported, flush=True)

    def begin(self, bridge: LineMonitorBridge) -> None:
        """Enrolment alone starts nothing."""


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


@dataclass(eq=False, repr=False)
class CliBridge(ExternalRuntime, DeliversInput, Generic[BridgeOptions]):
    """Core delivery and lifetime shared by CLI-specific transports."""

    state_directory: ClassVar[Path]

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        """Declare the options this harness's transport needs."""

    @classmethod
    def environment_session(cls) -> str:
        raise ValueError('Supply --session or a complete bus binding')

    options: BridgeOptions
    address: BindingAddress
    db: sqlite3.Connection
    binding: Binding = field(init=False)

    @cached_property
    def comms(self) -> Comms:
        return wire(Path(self.address.root))

    def watch_record(self, binding: SessionBinding) -> WatchRecord:
        return WatchRecord(binding.bus, binding.root,
                           (Path(sys.argv[0]).name, *sys.argv[1:]), self.options.base_state)

    def session_process(self) -> ProcessIdentity:
        """A line-monitor endpoint lives in the session-owned bridge process."""
        return ProcessIdentity.capture(os.getpid())

    async def session_worktree(self) -> Path:
        return Path.cwd()

    async def registered_watch(self, operation: Inbox) -> None:
        with ExitStack() as endpoint:
            with operation.claimed(WatchClaim(self.binding.thread), self.watch_record(self.binding)) as held:
                try:
                    async with asyncio.TaskGroup() as tasks:
                        await operation.start(self, tasks, endpoint)
                finally:
                    # A pause ends the tasks by cancellation; the claim is kept either way.
                    operation.keep(self, held)

    async def watch_operation(self, operation: Watch) -> None:
        await self.registered_watch(operation)

    @abstractmethod
    async def show_sent(self, result: SendResult) -> None:
        """Display a completed send without publishing it again."""

    async def report_failure(self, text: str) -> None:
        self.log(text)

    def sent_text(self, result: SendResult) -> str:
        message = result.message
        return '\n'.join((f'Comms out · {message.sender} → {message.target}',
                          message.body, f'Message ID: {message.message_id}', *result.notices))

    @staticmethod
    def log(text: str) -> None:
        print(text, file=sys.stderr, flush=True)

    async def prepare_delivery(self) -> None:
        """A transport may prepare its harness after the operation bound its reader."""

    async def deliver_board(self, update: CommanderUpdate, message: messages.Message) -> type[Outcome]:
        """A harness with no tool-call route takes a heartbeat packet like any message."""
        return await self.deliver(message)

    async def accept(self, message: messages.Message) -> None:
        conversation = message.type.conversation_message
        outcome = UnconfirmedOutcome if conversation else FilteredOutcome
        delivery = DeliveryRow(message.message_id, FieldCodec.compact(message.to_wire()) + '\n', outcome)
        # The thread's claim leaves the native queue before the journal row exists,
        # so no crash between them lets a later native owner replay this message.
        await self.comms.bus.when_open(partial(
            self.comms.bus.hand_to_harness, self.binding.bus, (message.reference,)))
        with self.db:
            first = delivery.insert_if_absent(self.db).rowcount
        if not first:
            return
        result = await message.task.delivered_by(self, message) if conversation else outcome
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
            '--after', json.dumps(FieldCodec.encode(
                DeliveryRow.resume_after(self.db, self.binding))),
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

    output_type: ClassVar[type[ThreadOutput]] = ThreadOutput

    async def publish_transcript(self) -> None:
        comms = self.comms
        output = self.output_type.for_comms(
            await asyncio.to_thread(comms.transcripts.external, self.binding.bus), comms)
        # This subscription can assemble only items whose start it receives.
        # A prefix from the previous subscription cannot stand for that stream.
        await comms.bus.when_open(output.transcript.end_turn)
        # Each CLI reports its own initial state. Disk writes never stop the
        # event reader from receiving a newer whole draft of the same item.
        queue: asyncio.Queue[RuntimeUpdate] = asyncio.Queue()

        async def read() -> None:
            try:
                async for update in self.transcript():
                    queue.put_nowait(await update.observed(self))
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


class MonitorFrame(TaggedWire, ABC, affix=""):
    """Our line protocol inside the harness's task-notification envelope."""
    @abstractmethod
    async def confirm(
        self, bridge: LineMonitorBridge, record: ObservedMonitor,
    ) -> tuple[str, ...]: ...


class MonitorProtocolFrame(MonitorFrame, declared_name=None):
    """A line the monitor writes itself; the transcript scan knows it by its protocol prefix."""
    protocol: Literal['agent-comms-monitor'] = 'agent-comms-monitor'
    wire_prefix: ClassVar[str] = '{"protocol":' + json.dumps(protocol) + ','

    def emit(self) -> None:
        sys.stdout.write(self.model_dump_json() + '\n')
        sys.stdout.flush()


class MonitorProbe(MonitorProtocolFrame, declared_name='probe'):
    session: str
    token: str

    async def confirm(self, bridge: LineMonitorBridge, record: ObservedMonitor) -> tuple[str, ...]:
        await bridge.confirm_probe(self)
        return ()


class MonitorBridgeStopped(MonitorProtocolFrame, declared_name='bridge-stopped'):
    """The reader's notice that no bridge holds its claim, so nothing is delivered until one starts."""
    notice: str

    @classmethod
    def announce(cls, claim: WatchClaim) -> None:
        """One Monitor event: the bridge is gone, so nothing arrives until it starts again."""
        stopped = cls(notice=(
            'Comms bridge stopped: no messages are delivered until it starts again. '
            f'{claim.path.with_suffix(".log")} names why; '
            'agent-comms-claude-bridge connect restarts it and this Monitor resumes.'))
        stopped.emit()

    async def confirm(self, bridge: LineMonitorBridge, record: ObservedMonitor) -> tuple[str, ...]:
        """A notice carries no delivery to confirm."""
        return ()


class MonitorBridgeSwapping(MonitorProtocolFrame, declared_name='bridge-swapping'):
    notice: str

    @classmethod
    def announce(cls, claim: WatchClaim) -> None:
        cls(notice=(
            f'Waiting for the session bridge of {claim.thread}: '
            'an install holds its claim and is replacing it.'
        )).emit()

    async def confirm(self, bridge: LineMonitorBridge, record: ObservedMonitor) -> tuple[str, ...]:
        """A notice carries no delivery to confirm."""
        return ()


class MonitorPart(MonitorFrame, declared_name='part'):
    id: InstanceOf[
        Annotated[
            MessageId,
            PlainValidator(partial(FieldCodec.decode, MessageId)),
            PlainSerializer(FieldCodec.encode),
        ]
    ]
    sender: str
    number: int = Field(ge=1)
    total: int = Field(ge=1)
    body: str

    @model_validator(mode='after')
    def valid_number(self):
        if self.number > self.total:
            raise ValueError('Monitor part number exceeds total')
        return self

    async def confirm(self, bridge: LineMonitorBridge, record: ObservedMonitor) -> tuple[str, ...]:
        targets = []
        for delivery in DeliveryRow.select(bridge.db, id=self.id):
            targets.extend(await delivery.monitor.confirm(bridge, delivery, self, record))
        return tuple(targets)


class ForeignMonitorFrame(MonitorFrame, declared_name='foreign'):
    text: str

    async def confirm(self, bridge: LineMonitorBridge, record: ObservedMonitor) -> tuple[str, ...]:
        """Another monitor's output is not comms delivery evidence."""
        return ()


MONITOR_FRAME = WireModel.adapter(MonitorFrame.declared_union())


@dataclass(frozen=True)
class MonitorObservation(RuntimeUpdate):
    frame: MonitorFrame
    record: ObservedMonitor
    targets: tuple[str, ...] = ()

    async def observed(self, bridge: LineMonitorBridge) -> RuntimeUpdate:
        return replace(self, targets=await self.frame.confirm(bridge, self.record))

    def publish(self, output: ThreadOutput) -> None:
        """Keep confirmed reply routes in transcript order, without a second input row."""
        output.delivered_targets(self.targets)


class TranscriptConfirmsDelivery:
    """Delivery is confirmed only by observing it in the bridge's own transcript subscription."""


class MonitorIncomingFrame(IncomingFrame):
    @property
    @abstractmethod
    def parts(self) -> tuple[str, ...]: ...


class LineMonitorBridge(TranscriptConfirmsDelivery, TranscriptBridge[Options]):
    """Session-owned subscription and delivery journal, with replaceable readers."""

    output_type: ClassVar[type[ThreadOutput]] = ReplyingThreadOutput
    incoming_frame: ClassVar[type[MonitorIncomingFrame]]
    window_bytes: ClassVar[int]

    def __init__(self, options: Options, address: BindingAddress, db: sqlite3.Connection) -> None:
        super().__init__(options, address, db)
        self.reader: ReaderState = NoReader()
        self.reader_changed = asyncio.Condition()
        self.retirement: BridgeExit = BridgeExit()

    async def declare_reader(self, status: ThreadStatus) -> None:
        await self.comms.bus.when_open(lambda: self.comms.registry.register(
            self.comms.registry.require(self.binding.bus), status))

    def watch_record(self, binding: SessionBinding) -> WatchRecord:
        command = (str(Path(sys.argv[0]).absolute()), Watch.__name__.lower(),
                   '--state', str(self.options.state.absolute()))
        return WatchRecord(binding.bus, binding.root, command, self.options.base_state)

    async def watch_operation(self, operation: Watch) -> None:
        await read_monitor(
            WatchClaim(self.binding.thread), MonitorBridgeStopped.announce,
            MonitorBridgeSwapping.announce,
        )

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
        await super().prepare_delivery()
        await self.reader.prepare_delivery(self)

    async def confirm_probe(self, probe: MonitorProbe) -> None:
        await self.reader.confirm_probe(self, probe)

    async def part_observed(self, message_id: MessageId, number: int) -> None:
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


def run_bridge(bridge_type: type[CliBridge[BridgeOptions]], options: BridgeOptions) -> None:
    sys.stderr = BridgeLog(sys.stderr)
    os.umask(0o077)
    try:
        with Binding.open(options.state) as db, asyncio.Runner() as runner:
            bridge = bridge_type(options, options.binding.bind(db), db)
            runner.run(options.operation.prepare(bridge))
            with suppress(asyncio.CancelledError):
                runner.run(bridge.run())
        options.operation.retired(bridge)
    except* BlockingIOError:
        sys.exit('Another watcher owns this conversation')
