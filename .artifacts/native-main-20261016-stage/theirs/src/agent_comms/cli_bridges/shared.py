"""Shared Core inbox delivery and process lifetime for external CLI adapters."""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import secrets
import os
import signal
import sys
from abc import ABC, abstractmethod
from contextlib import aclosing, suppress
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import AsyncIterator, ClassVar, Generic, Literal, Self, TypeVar

from pydantic import JsonValue, TypeAdapter, Field, model_validator
from metaclass_registry import RegistryConfig

from agent_comms.activity import ActivityState
from agent_comms.agent_activity import AgentActivity
from agent_comms.child_process import ProcessIdentity
from agent_comms.cli_protocol import CORE_ERROR, CoreError, TaggedWire, WireModel
from agent_comms.declared_family import DeclaredFamily
from agent_comms.coordination_database import CoordinationStore
from agent_comms.comms import Comms
from agent_comms.external_transcript import ExternalTranscript
from agent_comms.field_codec import FieldCodec
from agent_comms.messages import Message, MessageKind, InfoKind
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.recorded_time import EventTime, UnrecordedEventTime
from agent_comms.read_basis import ExplicitWatchAcknowledgment
from agent_comms.thread_identity import ThreadRole
from agent_comms.tool_declaration import ToolDeclaration
from agent_comms.tool_results import ToolDiff
from agent_comms.transcript_events import TextTranscript, ToolEndTranscript, ToolStartTranscript, TranscriptEvent
from agent_comms.typed_table import Column, TypedTable


class SendResult(WireModel):
    id: str

@dataclass(frozen=True)
class ToolCall:
    """A CLI invocation uses the same preview and history as a native tool."""
    id: str
    name: str
    arguments: JsonValue
    detail: str = ""

    @property
    def title(self) -> str:
        return ToolDeclaration.caption(self.name, self.detail)

    def finished(self, text: str, ok: bool, *, diff: ToolDiff | None = None) -> tuple[TranscriptEvent, ...]:
        return (ToolStartTranscript(tool_call_id=self.id, tool_name=self.name,
                                    raw_input=self.arguments, detail=self.detail),
                ToolEndTranscript(tool_call_id=self.id, tool_name=self.name,
                                  text=text, ok=ok, diff=diff))


@dataclass(frozen=True)
class Binding(TypedTable):
    root: str
    bus: str
    thread: str


@dataclass
class DraftAccumulator:
    """One subscription's item prefix; the shared output owns its lifetime."""

    started_at: EventTime

    def restarted(self, replacement: DraftAccumulator) -> DraftAccumulator:
        return replacement

    def started(self, update: StartedItem) -> tuple[TranscriptUpdate, ...]:
        return (*self.activity(ActiveItem(update.turn_id, update.item_id, update.state, update.detail)),
                *self.snapshot(update.turn_id, update.item_id))

    def activity(self, update: ActiveItem) -> tuple[TranscriptUpdate, ...]:
        return (update,)

    def snapshot(self, turn_id: str, item_id: str) -> tuple[DraftItem, ...]:
        return ()

    def append(self, text: str, section: int) -> None:
        raise ValueError('Received a text delta for a non-text item')

    def finish(self, update: CompletedItem) -> tuple[ClosedDraft, FinishedItem]:
        """Close on a full item, preserving provider time when no start was observed."""
        events = tuple(self.started_at.stamp(event) for event in update.events)
        return ClosedDraft(self.started_at), FinishedItem(update.turn_id, update.item_id, events)


class NoPrefixDraft(DraftAccumulator):
    """No observed prefix; retain no per-item state so the instance can be shared."""

    def __init__(self) -> None:
        super().__init__(UnrecordedEventTime())

    def append(self, text: str, section: int) -> None:
        """Drop the suffix: without an observed prefix it cannot form a whole item."""


NO_PREFIX = NoPrefixDraft()


@dataclass
class TextDraft(DraftAccumulator):
    event_type: type[TextTranscript]
    sections: dict[int, list[str]]
    separator: ClassVar[str] = ''

    @classmethod
    def from_parts(cls, event_type: type[TextTranscript], parts: Iterable[str], started_at: EventTime) -> Self:
        return cls(started_at, event_type, {index: [text] for index, text in enumerate(parts)})

    def append(self, text: str, section: int) -> None:
        self.sections.setdefault(section, []).append(text)

    @property
    def events(self) -> tuple[TranscriptEvent, ...]:
        text = self.separator.join(text for index in sorted(self.sections)
                                   if (text := ''.join(self.sections[index])))
        return (self.started_at.stamp(self.event_type(text)),) if text else ()

    def snapshot(self, turn_id: str, item_id: str) -> tuple[DraftItem, ...]:
        return (DraftItem(turn_id, item_id, self),)


class ParagraphDraft(TextDraft):
    separator = '\n\n'


class ClosedDraft(DraftAccumulator):
    """Completed snapshots reject overlapping older starts and late suffixes."""

    def restarted(self, replacement: DraftAccumulator) -> DraftAccumulator:
        return self

    def activity(self, update: ActiveItem) -> tuple[TranscriptUpdate, ...]:
        return ()

    def append(self, text: str, section: int) -> None:
        pass


@dataclass(frozen=True)
class ThreadOutput:
    """Where a CLI's output lands in Core: finished items and current activity."""
    transcript: ExternalTranscript
    agents: AgentActivity
    drafts: dict[tuple[str, str], DraftAccumulator] = field(default_factory=dict)

    def publish_batch(self, updates: list[TranscriptUpdate]) -> None:
        assembled = [part for update in updates for part in update.accumulate(self)]
        for update in TranscriptUpdate.coalesced(assembled):
            update.publish(self)

    def draft(self, key: tuple[str, str]) -> DraftAccumulator:
        return self.drafts.get(key, NO_PREFIX)

    def start_item(self, update: StartedItem) -> tuple[TranscriptUpdate, ...]:
        key = update.turn_id, update.item_id
        current = self.draft(key).restarted(update.accumulator)
        self.drafts[key] = current
        return current.started(update)

    def active_item(self, update: ActiveItem) -> tuple[TranscriptUpdate, ...]:
        return self.draft((update.turn_id, update.item_id)).activity(update)

    def append_item(self, update: AppendDraftItem) -> tuple[TranscriptUpdate, ...]:
        current = self.draft((update.turn_id, update.item_id))
        current.append(update.text, update.section)
        return current.snapshot(update.turn_id, update.item_id)

    def finish_item(self, update: CompletedItem) -> FinishedItem:
        key = update.turn_id, update.item_id
        self.drafts[key], finished = self.draft(key).finish(update)
        return finished

    def end_turn(self, turn_id: str) -> None:
        for key in tuple(self.drafts):
            if key[0] == turn_id:
                del self.drafts[key]

    def end_all_turns(self) -> None:
        self.drafts.clear()

    def status(self, state: ActivityState, detail: str = '') -> None:
        detail = ' '.join(detail.split())
        self.agents.set_activity(self.transcript.incarnation.name, state, detail)


@dataclass(frozen=True)
class TranscriptUpdate(ABC):
    """One change a CLI reports about its thread."""
    is_status: ClassVar[bool] = False

    @abstractmethod
    def publish(self, output: ThreadOutput) -> None: ...

    def accumulate(self, output: ThreadOutput) -> tuple[TranscriptUpdate, ...]:
        return (self,)

    async def observed(self, bridge: TranscriptBridge) -> None:
        """A source-owned notification may confirm delivery before publication."""

    def retain_before(self, later: set[tuple[str, str]]) -> bool:
        """Whether this update still needs publication before later updates."""
        return True

    @staticmethod
    def coalesced(updates: list[TranscriptUpdate]) -> list[TranscriptUpdate]:
        later: set[tuple[str, str]] = set()
        kept = [update for update in reversed(updates) if update.retain_before(later)]
        return list(reversed(kept))


@dataclass(frozen=True)
class TurnStarted(TranscriptUpdate):
    is_status = True
    detail: str = ''

    def publish(self, output: ThreadOutput) -> None:
        output.status(ActivityState.WORKING, self.detail)


@dataclass(frozen=True)
class TurnEnded(TranscriptUpdate):
    is_status = True

    def publish(self, output: ThreadOutput) -> None:
        output.transcript.end_turn()
        output.status(ActivityState.IDLE)


@dataclass(frozen=True)
class ActiveItem(TranscriptUpdate):
    """An item the CLI started producing: what it is doing now."""
    is_status = True
    turn_id: str
    item_id: str
    state: ActivityState
    detail: str

    def publish(self, output: ThreadOutput) -> None:
        output.status(self.state, self.detail)

    def accumulate(self, output: ThreadOutput) -> tuple[TranscriptUpdate, ...]:
        return output.active_item(self)


@dataclass(frozen=True)
class FinishedItem(TranscriptUpdate):
    """One CLI item's complete presentation, identified by its producer."""
    turn_id: str
    item_id: str
    events: tuple[TranscriptEvent, ...]

    def publish(self, output: ThreadOutput) -> None:
        output.transcript.finish(turn_id=self.turn_id, item_id=self.item_id, events=self.events)

    def retain_before(self, later: set[tuple[str, str]]) -> bool:
        later.add((self.turn_id, self.item_id))
        return True


@dataclass(frozen=True)
class DraftItem(TranscriptUpdate):
    """A pending view; build its text only if this preview survives coalescing."""
    turn_id: str
    item_id: str
    accumulator: TextDraft

    @property
    def events(self) -> tuple[TranscriptEvent, ...]:
        return self.accumulator.events

    def publish(self, output: ThreadOutput) -> None:
        output.transcript.draft(turn_id=self.turn_id, item_id=self.item_id, events=self.events)

    def retain_before(self, later: set[tuple[str, str]]) -> bool:
        key = self.turn_id, self.item_id
        keep = key not in later
        later.add(key)
        return keep


class DraftUpdate(TranscriptUpdate):
    """Part decoding enters the same accumulation path even for direct publication."""

    @abstractmethod
    def accumulate(self, output: ThreadOutput) -> tuple[TranscriptUpdate, ...]: ...

    def publish(self, output: ThreadOutput) -> None:
        output.publish_batch([self])


@dataclass(frozen=True)
class CompletedItem(DraftUpdate, FinishedItem):
    def accumulate(self, output: ThreadOutput) -> tuple[TranscriptUpdate, ...]:
        return (output.finish_item(self),)


@dataclass(frozen=True)
class StartedItem(DraftUpdate, ActiveItem):
    accumulator: DraftAccumulator

    def accumulate(self, output: ThreadOutput) -> tuple[TranscriptUpdate, ...]:
        return output.start_item(self)


@dataclass(frozen=True)
class AppendDraftItem(DraftUpdate):
    turn_id: str
    item_id: str
    text: str
    section: int = 0

    def accumulate(self, output: ThreadOutput) -> tuple[TranscriptUpdate, ...]:
        return output.append_item(self)


@dataclass(frozen=True)
class FinishedTurn(DraftUpdate, TurnEnded):
    turn_id: str

    def accumulate(self, output: ThreadOutput) -> tuple[TranscriptUpdate, ...]:
        output.end_turn(self.turn_id)
        return (TurnEnded(),)


@dataclass(frozen=True)
class AllTurnsEnded(DraftUpdate, TurnEnded):
    def accumulate(self, output: ThreadOutput) -> tuple[TranscriptUpdate, ...]:
        output.end_all_turns()
        return (TurnEnded(),)


@dataclass
class Options:
    operation: Operation
    root: str | None
    bus_thread: str | None
    session: str | None
    state: Path

class Outcome(DeclaredFamily, affix='Outcome'):
    """Stored spellings stay the same; their owners perform confirmation."""

    @classmethod
    async def acknowledge(cls, core: CoreConnection, message: Message) -> None:
        """Pending transport outcomes cannot advance Core's read state."""

    @classmethod
    async def observe_monitor(cls, bridge: LineMonitorBridge, message: Message,
                              record: ObservedMonitor) -> None:
        await bridge.finish_delivery(message, DeliveredOutcome, record)


class FilteredOutcome(Outcome):
    @classmethod
    async def acknowledge(cls, core: CoreConnection, message: Message) -> None:
        await core.acknowledge(message)


class UnconfirmedOutcome(Outcome):
    pass


class DeliveredOutcome(FilteredOutcome):
    @classmethod
    async def observe_monitor(cls, bridge: LineMonitorBridge, message: Message,
                              record: ObservedMonitor) -> None:
        """Reopened history preserves the first confirmation record and time."""


class EmittedOutcome(UnconfirmedOutcome):
    pass

@dataclass(frozen=True)
class DeliveryRow(TypedTable):
    id: str = field(metadata={"sql": Column(primary_key=True)})
    payload: str
    outcome: type[Outcome] = field(metadata={"sql": Column(index=True)})


@dataclass(frozen=True)
class MonitorEvidence(DeclaredFamily):
    """Actual session observations, retained in the existing delivery journal."""

    def record(self, journal: DeliveryJournal, message_id: str) -> None:
        """An API/pipe outcome does not invent a session record."""

    def accepted(self, total: int) -> MonitorEvidence:
        return ReceivingMonitor(total - 1)

    async def confirm(self, bridge: LineMonitorBridge, attempt: MonitorAttemptRow,
                      record: ObservedMonitor) -> None:
        """Only a complete set of observed parts can confirm delivery."""


@dataclass(frozen=True)
class UnrecordedMonitor(MonitorEvidence):
    pass


class StoredMonitorEvidence:
    def record(self, journal: DeliveryJournal, message_id: str) -> None:
        MonitorAttemptRow.update(journal.db, where='id=?', parameters=(message_id,), evidence=self)


@dataclass(frozen=True)
class ReceivingMonitor(StoredMonitorEvidence, MonitorEvidence):
    remaining: int

    def accepted(self, total: int) -> MonitorEvidence:
        return ReceivingMonitor(self.remaining - 1)

    async def confirm(self, bridge: LineMonitorBridge, attempt: MonitorAttemptRow,
                      record: ObservedMonitor) -> None:
        if self.remaining:
            return
        # Decode the original body once, only after every indexed part arrived.
        delivery, = DeliveryRow.select(bridge.journal.db, id=attempt.id)
        message = Message.from_wire(json.loads(delivery.payload))
        parts = MonitorPartRow.select(bridge.journal.db, id=attempt.id, order_by=('number',))
        if (''.join(part.body for part in parts) != message.body
                or any(part.sender != message.sender for part in parts)):
            with bridge.journal.db:
                MismatchedMonitor('Observed parts do not equal the original message').record(
                    bridge.journal, attempt.id)
            print(f'Monitor parts mismatch; message remains pending: {attempt.id}',
                  file=sys.stderr, flush=True)
            return
        await delivery.outcome.observe_monitor(bridge, message, record)


@dataclass(frozen=True)
class MismatchedMonitor(StoredMonitorEvidence, MonitorEvidence):
    reason: str


@dataclass(frozen=True)
class ObservedMonitor(StoredMonitorEvidence, MonitorEvidence):
    record_id: str
    timestamp: float

    def accepted(self, total: int) -> MonitorEvidence:
        return self  # Preserve the first complete observation across startup overlap.


@dataclass(frozen=True)
class MonitorPartRow(TypedTable):
    id: str = field(metadata={'sql': Column(primary_key=True)})
    number: int = field(metadata={'sql': Column(primary_key=True)})
    sender: str
    body: str
    record_id: str
    timestamp: float


class MonitorAttempt(ABC):
    @abstractmethod
    async def confirm(self, bridge: LineMonitorBridge, notification: MonitorPart,
                      record: ObservedMonitor) -> None: ...


class NoMonitorAttempt(MonitorAttempt):
    async def confirm(self, bridge: LineMonitorBridge, notification: MonitorPart,
                      record: ObservedMonitor) -> None:
        """Historical or unrelated emissions have no matching new attempt."""


NO_MONITOR_ATTEMPT = NoMonitorAttempt()


@dataclass(frozen=True)
class MonitorAttemptRow(TypedTable, MonitorAttempt):
    id: str = field(metadata={'sql': Column(primary_key=True)})
    parts: int
    evidence: MonitorEvidence = field(default_factory=UnrecordedMonitor)

    async def confirm(self, bridge: LineMonitorBridge, notification: MonitorPart,
                      record: ObservedMonitor) -> None:
        observed = MonitorPartRow(self.id, notification.number, notification.sender,
                                  notification.body, record.record_id, record.timestamp)
        evidence = self.evidence
        with bridge.journal.db:
            if observed.insert_if_absent(bridge.journal.db).rowcount:
                evidence = evidence.accepted(self.parts)
                evidence.record(bridge.journal, self.id)
        await evidence.confirm(bridge, self, record)
        bridge.part_observed(self.id, notification.number)


class DeliveryJournal:
    """Transport outcomes only. Core still owns messages and inbox routing."""

    def __init__(self, state: Path) -> None:
        self.path = state / 'delivery.sqlite3'
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = CoordinationStore.writer(self.path)
        try:
            self.db.execute('PRAGMA synchronous=FULL')
            for row in (Binding, DeliveryRow, MonitorAttemptRow, MonitorPartRow):
                row.create(self.db, if_not_exists=True)
            self.db.commit()
        except BaseException:
            self.db.close()
            raise

    def bind(self, requested: Binding | None) -> Binding:
        with self.db:
            if requested is not None:
                # Bind before the later watch lock, including non-watch callers.
                # SQLite must serialize the decision as well as its insertion.
                self.db.execute('BEGIN IMMEDIATE')
            rows = Binding.select(self.db)
            if not rows and requested is not None:
                requested.insert(self.db)
                return requested
            if len(rows) != 1:
                raise ValueError('Supply one complete Core/CLI binding for this state directory')
            current = rows[0]
            if requested is not None and requested != current:
                raise ValueError('This state directory belongs to another Core/CLI conversation')
            return current

    def begin(self, message: Message, payload: str, outcome: type[Outcome]) -> bool:
        with self.db:
            return DeliveryRow(message.message_id, payload, outcome).insert_if_absent(self.db).rowcount == 1

    def finish(self, message: Message, outcome: type[Outcome],
               evidence: MonitorEvidence = UnrecordedMonitor()) -> None:
        with self.db:
            evidence.record(self, message.message_id)
            DeliveryRow.update(self.db, where="id=?", parameters=(message.message_id,), outcome=outcome)

    def outcome(self, message: Message) -> type[Outcome]:
        row, = DeliveryRow.select(self.db, where='id=?', parameters=(message.message_id,))
        return row.outcome

    def emitting(self, message: Message, parts: int) -> None:
        with self.db:
            MonitorAttemptRow(message.message_id, parts).insert(self.db)

    def monitor_attempt(self, notification: MonitorPart) -> MonitorAttempt:
        attempts = MonitorAttemptRow.select(self.db, id=notification.id, parts=notification.total)
        return next(iter(attempts), NO_MONITOR_ATTEMPT)

    def report_unconfirmed(self) -> None:
        pending = tuple(outcome.declared_name for outcome in Outcome.members_with(UnconfirmedOutcome))
        placeholders = ','.join('?' for _ in pending)
        rows = DeliveryRow.column_values(self.db, "id", where=f"outcome IN ({placeholders})",
                                         parameters=pending)
        if rows:
            print(f'Unconfirmed messages retained without replay: {rows}',
                  file=sys.stderr, flush=True)

    def close(self) -> None:
        self.db.close()

class CoreConnection:
    """Core owns publication; its CLI owns the long-lived watch stream."""

    def __init__(self, executable: str, binding: Binding) -> None:
        self.executable = executable
        self.binding = binding

    @cached_property
    def comms(self) -> Comms:
        return Comms(Path(self.binding.root))

    async def send(self, target: str, body: str, kind: type[MessageKind]) -> SendResult:
        message_id = await asyncio.to_thread(
            self.comms.messaging.send, self.binding.bus, target, body, kind)
        return SendResult(id=message_id)

    async def watch(self, state: Path) -> AsyncIterator[tuple[Message, str] | CoreError]:
        with (state / 'watch.stderr.log').open('a') as errors:
            child = await asyncio.create_subprocess_exec(
                self.executable, '--root', self.binding.root, 'watch', '--thread', self.binding.bus,
                '--acknowledgment', FieldCodec.encode(ExplicitWatchAcknowledgment),
                stdout=asyncio.subprocess.PIPE, stderr=errors)
            try:
                while line := await child.stdout.readline():
                    wire = json.loads(line)
                    if isinstance(wire, dict) and 'error' in wire:
                        yield CORE_ERROR.validate_python(wire)
                        return
                    yield Message.from_wire(wire), line.decode()
                raise RuntimeError(f'Core watch ended, exit {await child.wait()}')
            finally:
                await self.join(child)

    async def acknowledge(self, message: Message) -> None:
        await asyncio.to_thread(self.comms.messaging.acknowledge, self.binding.bus, (message.reference,))

    @staticmethod
    async def join(child: asyncio.subprocess.Process) -> None:
        # An exited child no longer accepts the signal.
        with suppress(ProcessLookupError):
            child.terminate()
        try:
            async with asyncio.timeout(5):
                await child.wait()
        except TimeoutError:
            child.kill()
            await child.wait()

BridgeOptions = TypeVar('BridgeOptions', bound=Options)
OperationBridge = TypeVar('OperationBridge', bound='CliBridge')


class Operation(ABC, Generic[OperationBridge]):
    """A decoded CLI command owns its execution."""

    def binding(self, options: Options) -> Binding | None:
        if any((options.root, options.bus_thread, options.session)):
            if not all((options.root, options.bus_thread, options.session)):
                raise ValueError('Supply all three binding arguments or use the saved binding')
            return Binding(options.root, options.bus_thread, options.session)
        return None

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        """Declare the options only this command takes."""

    @classmethod
    @abstractmethod
    def from_arguments(cls, arguments: argparse.Namespace) -> Operation: ...

    @abstractmethod
    async def run(self, bridge: OperationBridge) -> None: ...


@dataclass(frozen=True)
class Send(Operation):
    target: str
    body: str
    kind: type[MessageKind]

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        parser.add_argument('--to', required=True)
        parser.add_argument('--body', required=True)
        parser.add_argument('--type', default=FieldCodec.encode(InfoKind), choices=MessageKind.sendable_names())

    @classmethod
    def from_arguments(cls, arguments: argparse.Namespace) -> Send:
        send = cls(arguments.to, arguments.body, FieldCodec.decode(type[MessageKind], arguments.type))
        del arguments.to, arguments.body, arguments.type
        return send

    async def run(self, bridge: CliBridge) -> None:
        result = await bridge.core.send(self.target, self.body, self.kind)
        await bridge.show_sent(result, self.target, self.body)


class Inbox(Operation[OperationBridge]):
    @classmethod
    def from_arguments(cls, arguments: argparse.Namespace) -> Inbox:
        return cls()

    def start(self, bridge: OperationBridge, tasks: asyncio.TaskGroup) -> None:
        tasks.create_task(bridge.watch())

    async def run(self, bridge: OperationBridge) -> None:
        # One session claim, independent of --state and bus aliases. This is the
        # existing watch lease, not a process file or a second registration.
        claims = Path.home() / '.local/state/agent-comms/cli-watch'
        claims.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = hashlib.sha256(bridge.binding.thread.encode()).hexdigest()
        with (claims / f'{key}.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                from agent_comms.registration import Registration

                registry = await asyncio.to_thread(
                    Registration, Path(bridge.binding.root) / 'registry.json')
                # The endpoint, not a particular CLI's record parser, owns presence.
                with registry.external_process(bridge.binding.bus, ProcessIdentity.capture(os.getpid())):
                    async with asyncio.TaskGroup() as tasks:
                        self.start(bridge, tasks)
            except ExceptionGroup as error:
                # Join all tasks, retaining the original single failure's exit policy.
                if len(error.exceptions) == 1:
                    raise error.exceptions[0] from error
                raise


class Watch(Inbox):
    def start(self, bridge: TranscriptBridge, tasks: asyncio.TaskGroup) -> None:
        super().start(bridge, tasks)
        tasks.create_task(bridge.publish_transcript())


@dataclass(frozen=True)
class IncomingFrame(ABC):
    message: Message

    @property
    def text(self) -> str:
        origin = 'human' if self.message.sender_role is ThreadRole.USER else 'external agent'
        return (f'Comms in · {self.message.sender} ({origin})\n'
                f'{self.message.body}\nMessage ID: {self.message.message_id}')

    @property
    @abstractmethod
    def parts(self) -> tuple[str, ...]: ...


class UnsplitFrame(IncomingFrame):
    @property
    def parts(self) -> tuple[str, ...]:
        return (self.text,)  # API transports accept the complete message in one request.


class CliBridge(ABC, Generic[BridgeOptions]):
    """Core delivery and lifetime shared by CLI-specific transports."""

    operations: ClassVar[tuple[type[Operation], ...]] = (Inbox, Send)
    incoming_frame: ClassVar[type[IncomingFrame]] = UnsplitFrame

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        """Declare the options this harness's transport needs."""

    def __init__(self, options: BridgeOptions, binding: Binding, journal: DeliveryJournal) -> None:
        self.options = options
        self.binding = binding
        self.journal = journal
        # The CLI of the runtime this bridge runs from.
        self.core = CoreConnection(str(LaunchRuntime.running().interpreter.with_name('agent-comms')), binding)

    @abstractmethod
    async def deliver(self, message: Message) -> type[Outcome]:
        """Return harness evidence; the shared watch owns completion."""

    @abstractmethod
    async def show_sent(self, result: SendResult, target: str, body: str) -> None:
        """Display a completed send without publishing it again."""

    @abstractmethod
    async def report_failure(self, text: str) -> None:
        """Report a bridge failure through the CLI's visible output."""

    def sent_text(self, result: SendResult, target: str, body: str) -> str:
        return (f'Comms out · {self.binding.bus} → {target}\n'
                f'{body}\nMessage ID: {result.id}')

    async def prepare_delivery(self) -> None:
        """API transports already bind the intended harness on each request."""

    async def settle(self, message: Message, result: type[Outcome]) -> None:
        await self.finish_delivery(message, result)

    async def finish_delivery(self, message: Message, outcome: type[Outcome],
                              evidence: MonitorEvidence = UnrecordedMonitor()) -> None:
        self.journal.finish(message, outcome, evidence)
        await outcome.acknowledge(self.core, message)

    async def watch(self) -> None:
        await self.prepare_delivery()
        self.journal.report_unconfirmed()
        print(f'Watching {self.binding.bus} → {self.binding.thread}', file=sys.stderr, flush=True)
        while True:
            async with aclosing(self.core.watch(self.options.state)) as stream:
                async for item in stream:
                    if isinstance(item, CoreError):
                        delay = item.watch_delay()
                        print(f'Core watch unavailable: {item.error}; reconnecting read stream',
                              file=sys.stderr, flush=True)
                        await asyncio.sleep(delay)
                        break
                    message, payload = item
                    conversation = message.type.conversation_message
                    outcome = UnconfirmedOutcome if conversation else FilteredOutcome
                    if not self.journal.begin(message, payload, outcome):
                        await self.journal.outcome(message).acknowledge(self.core, message)
                        continue
                    if not conversation:
                        await self.finish_delivery(message, outcome)
                        continue
                    result = await self.deliver(message)
                    await self.settle(message, result)
                    print(f'{result.declared_name}: {message.message_id} from {message.sender}',
                          file=sys.stderr, flush=True)

    async def run(self) -> None:
        try:
            await self.options.operation.run(self)
        except (BlockingIOError, asyncio.CancelledError):
            raise
        except Exception as error:
            failure = f'Comms bridge stopped: {type(error).__name__}: {error}'
            print(failure, file=sys.stderr, flush=True)
            try:
                await self.report_failure(failure + '\nNo message was retried.')
            except Exception as notification_error:
                print(f'Failure display also failed: {notification_error}', file=sys.stderr)
            raise


class TranscriptBridge(CliBridge[BridgeOptions]):
    """A CLI with an event subscription also publishes whole transcript items."""

    operations = (*CliBridge.operations, Watch)

    @abstractmethod
    def transcript(self) -> AsyncIterator[TranscriptUpdate]:
        """Observe CLI output without submitting inputs or executing tools."""

    async def publish_transcript(self) -> None:
        comms = self.core.comms
        output = ThreadOutput(
            await asyncio.to_thread(comms.transcripts.external, self.binding.bus), comms.agents)
        # This subscription can assemble only items whose start it receives.
        # A prefix from the previous subscription cannot stand for that stream.
        await asyncio.to_thread(output.transcript.end_turn)
        # Each CLI reports its own initial state. Disk writes never stop the
        # event reader from receiving a newer whole draft of the same item.
        queue: asyncio.Queue[TranscriptUpdate | None] = asyncio.Queue()

        async def read() -> None:
            async for update in self.transcript():
                await update.observed(self)
                queue.put_nowait(update)
            queue.put_nowait(None)

        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(read())
            ended = False
            while not ended:
                pending = [await queue.get()]
                while not queue.empty():
                    pending.append(queue.get_nowait())
                ended = pending[-1] is None
                updates = pending[:-1] if ended else pending
                await asyncio.to_thread(output.publish_batch, updates)

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
        await bridge.journal.monitor_attempt(self).confirm(bridge, self, record)


class ForeignMonitorFrame(MonitorFrame):
    wire_tag = 'foreign'
    text: str

    async def confirm(self, bridge: LineMonitorBridge, record: ObservedMonitor) -> None:
        """Another monitor's output is not comms delivery evidence."""


MONITOR_FRAME = TypeAdapter(MonitorFrame.declared_union())


@dataclass(frozen=True)
class MonitorObservation(TranscriptUpdate):
    frame: MonitorFrame
    record: ObservedMonitor

    async def observed(self, bridge: LineMonitorBridge) -> None:
        await self.frame.confirm(bridge, self.record)

    def publish(self, output: ThreadOutput) -> None:
        """Delivery is not a second conversation item."""


class LineMonitorBridge(TranscriptBridge[Options]):
    """Monitor delivery requires the same live transcript subscription."""

    operations = (Send, Watch)

    def __init__(self, options: Options, binding: Binding, journal: DeliveryJournal) -> None:
        super().__init__(options, binding, journal)
        self.probe = MonitorProbe(session=binding.thread, token=secrets.token_hex(24))
        self.monitor_ready = asyncio.Event()
        self.part_events: dict[tuple[str, int], asyncio.Event] = {}

    async def prepare_delivery(self) -> None:
        print(self.probe.model_dump_json(), flush=True)
        print('Waiting for this session to observe the monitor probe; inbox remains unread.',
              file=sys.stderr, flush=True)
        # The harness owns notification scheduling. Missing proof never opens
        # the inbox; the watch's existing cancellation owns this wait's lifetime.
        await self.monitor_ready.wait()

    def confirm_probe(self, probe: MonitorProbe) -> None:
        if probe == self.probe:
            self.monitor_ready.set()

    async def settle(self, message: Message, result: type[Outcome]) -> None:
        return  # The session evidence already completed delivery.

    def part_observed(self, message_id: str, number: int) -> None:
        # An observation with no waiter is a startup replay of a finished part.
        if event := self.part_events.get((message_id, number)):
            event.set()

    @staticmethod
    def emit(text: str) -> None:
        # One reversible JSON string per line; no truncation or lost line breaks.
        print(json.dumps(text, ensure_ascii=False), flush=True)

    async def deliver(self, message: Message) -> type[Outcome]:
        parts = self.incoming_frame(message).parts
        self.journal.emitting(message, len(parts))
        for number, part in enumerate(parts, 1):
            event = asyncio.Event()
            key = message.message_id, number
            self.part_events[key] = event
            try:
                self.emit(part)
                self.journal.finish(message, EmittedOutcome)
                # Monitor may combine stdout writes into one capped event. The
                # session's observation, not a timer, releases the next part.
                await event.wait()
            finally:
                del self.part_events[key]
        return self.journal.outcome(message)

    async def show_sent(self, result: SendResult, target: str, body: str) -> None:
        self.emit(self.sent_text(result, target, body))

    async def report_failure(self, text: str) -> None:
        self.emit(text)

    async def watch(self) -> None:
        try:
            await super().watch()
        finally:
            self.emit('Comms monitor ended. No message was retried.')


def shared_parser(description: str | None, state: Path,
                  bridge_type: type[CliBridge]) -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--root')
    common.add_argument('--bus-thread')
    common.add_argument('--session')
    common.add_argument('--state', type=Path, default=state)
    bridge_type.add_arguments(common)
    parser = argparse.ArgumentParser(description=description)
    commands = parser.add_subparsers(dest='command', required=True)
    for operation in bridge_type.operations:
        command = commands.add_parser(operation.__name__.lower(), parents=[common])
        operation.add_arguments(command)
        command.set_defaults(operation=operation)
    return parser


def parse_options(parser: argparse.ArgumentParser,
                  options_type: type[BridgeOptions]) -> BridgeOptions:
    arguments = parser.parse_args()
    del arguments.command
    arguments.operation = arguments.operation.from_arguments(arguments)
    return options_type(**vars(arguments))


def run_bridge(bridge_type: type[CliBridge[BridgeOptions]], options: BridgeOptions) -> None:
    requested = options.operation.binding(options)
    os.umask(0o077)
    journal = DeliveryJournal(options.state)
    try:
        bridge = bridge_type(options, journal.bind(requested), journal)
        with asyncio.Runner() as runner:
            async def interruptible() -> None:
                current = asyncio.current_task()
                assert current is not None
                asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, current.cancel)
                await bridge.run()
            runner.run(interruptible())
    except asyncio.CancelledError:
        pass
    except BlockingIOError:
        sys.exit('Another watcher owns this conversation')
    finally:
        journal.close()
