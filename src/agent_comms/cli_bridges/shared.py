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
import traceback
from abc import ABC, abstractmethod
from contextlib import closing, contextmanager
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator, ClassVar, Generic, Literal, Self, TypeVar, Unpack

from pydantic import AliasChoices, ConfigDict, JsonValue, TypeAdapter, Field, model_validator
from metaclass_registry import RegistryConfig

from agent_comms.activity import ActivityState
from agent_comms.agent_activity import AgentActivity
from agent_comms.child_process import AttachedChild, ProcessIdentity, StreamingChildStdio
from agent_comms.cli_protocol import (
    DeliveredOutcome, EarlierItems, EmittedOutcome, FilteredOutcome, Outcome, TaggedWire, WireModel,
    UnconfirmedOutcome, WatchLine, raise_child_exit,
)
from agent_comms.declared_family import DeclaredFamily
from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_errors import CoordinationError
from agent_comms.comms import Comms
from agent_comms.external_transcript import ExternalPage, ExternalTranscript
from agent_comms.field_codec import FieldCodec
from agent_comms.native_tools import (
    BashTool, EditTool, GlobTool, GrepTool, ReadTool, ReplacementEdit, WriteTool)
from agent_comms.messages import Message, MessageKind, InfoKind
from agent_comms.message_reference import MessageReference
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.maintenance_barrier import MaintenanceHeldError
from agent_comms.recorded_time import EventTime, UnrecordedEventTime
from agent_comms.read_basis import ExplicitWatchAcknowledgment
from agent_comms.tool_declaration import ToolDeclaration
from agent_comms.tool_results import ToolDiff
from agent_comms.transcript_events import TextTranscript, ToolEndTranscript, ToolStartTranscript, TranscriptEvent
from agent_comms.typed_table import Column, TypedTable

if TYPE_CHECKING:
    from agent_comms.importing import ImportBuffer
    from .claude_protocol import ClaudeReaderPhase


class CliToolArguments(WireModel):
    """A CLI's arguments for one Core tool in its own spelling (Claude Code's, OpenCode's), read
    once into the Core record; the CLI's other keys stay in its raw input."""
    cli_names: ClassVar[tuple[str, ...]] = ()
    types: ClassVar[dict[str, type[CliToolArguments]]] = {}
    model_config = {'extra': 'allow'}

    def __init_subclass__(cls, **kwargs: Unpack[ConfigDict]) -> None:
        super().__init_subclass__(**kwargs)
        cls.types.update(dict.fromkeys(cls.cli_names, cls))

    def diff(self) -> ToolDiff | None:
        return None

    @abstractmethod
    def record(self) -> ToolDeclaration: ...

    @classmethod
    def record_of(cls, name: str, arguments: dict[str, JsonValue]) -> ToolDeclaration | None:
        try:
            return cls.types[name].model_validate(arguments).record()
        except (KeyError, ValueError):  # no shape for the name, or the values are refused
            return None


class CliCommandArguments(CliToolArguments):
    cli_names = ('Bash', 'bash')
    command: str

    def record(self) -> BashTool:
        return BashTool(command=self.command)


class CliPatternArguments(CliToolArguments):
    cli_names: ClassVar[tuple[str, ...]] = ('Grep', 'grep')
    pattern: str
    path: str | None = None

    def record(self) -> ToolDeclaration:
        return GrepTool(pattern=self.pattern, path=self.path)


class CliGlobArguments(CliPatternArguments):
    cli_names = ('Glob', 'glob')

    def record(self) -> GlobTool:
        return GlobTool(pattern=self.pattern, path=self.path)


class CliPathArguments(CliToolArguments):
    path: str = Field(validation_alias=AliasChoices('file_path', 'filePath'))


class CliReadArguments(CliPathArguments):
    cli_names = ('Read', 'read')

    def record(self) -> ReadTool:
        return ReadTool(path=self.path)


class CliReplacement(WireModel):
    old_string: str = Field(validation_alias=AliasChoices('old_string', 'oldString'))
    new_string: str = Field(validation_alias=AliasChoices('new_string', 'newString'))


class CliReplacingArguments(CliPathArguments):
    """A file edit by replacements, however many the CLI's call shape carries."""

    @abstractmethod
    def replacements(self) -> Sequence[CliReplacement]: ...

    def record(self) -> EditTool:
        return EditTool(path=self.path, edits=tuple(ReplacementEdit(
            old_text=edit.old_string, new_text=edit.new_string) for edit in self.replacements()))

    def diff(self) -> ToolDiff:
        return ToolDiff(''.join(patch.text for edit in self.replacements()
            if (patch := ToolDiff.from_versions(self.path, edit.old_string, edit.new_string))))


class CliEditArguments(CliReplacement, CliReplacingArguments):
    """One replacement, its fields in the call itself (Claude Code's Edit, OpenCode's edit)."""
    cli_names = ('Edit', 'edit')

    def replacements(self) -> Sequence[CliReplacement]:
        return (self,)


class CliMultiEditArguments(CliReplacingArguments):
    """A list of replacements (Claude Code's MultiEdit)."""
    cli_names = ('MultiEdit',)
    edits: list[CliReplacement]

    def replacements(self) -> Sequence[CliReplacement]:
        return self.edits


class CliWriteArguments(CliPathArguments):
    cli_names = ('Write', 'write')
    content: str

    def record(self) -> WriteTool:
        return WriteTool(path=self.path, content=self.content)

    def diff(self) -> ToolDiff | None:
        return ToolDiff.from_versions(self.path, '', self.content,
                                     before_path=self.path + ' (previous content not supplied)')


@dataclass(frozen=True)
class ToolCall:
    """A CLI invocation uses the same preview and history as a native tool: its arguments as the
    CLI sent them, and the Core tool record its bridge decoded them into once."""
    id: str
    name: str
    arguments: dict[str, JsonValue]
    diff: ToolDiff | None = None
    tool: ToolDeclaration | None = None

    @property
    def title(self) -> str:
        return ToolDeclaration.caption(self.name, self.tool)

    def finished(self, text: str, ok: bool) -> tuple[TranscriptEvent, ...]:
        return (ToolStartTranscript(tool_call_id=self.id, tool_name=self.name,
                                    raw_input=self.arguments, tool=self.tool),
                ToolEndTranscript(tool_call_id=self.id, tool_name=self.name,
                                  text=text, ok=ok, diff=self.diff))


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

    def during(self, phase: ClaudeReaderPhase) -> tuple[TranscriptUpdate, ...]:
        return (self,)

    @abstractmethod
    def publish(self, output: ThreadOutput) -> None: ...

    def accumulate(self, output: ThreadOutput) -> tuple[TranscriptUpdate, ...]:
        return (self,)

    async def observed(self, bridge: TranscriptBridge) -> None:
        """A source-owned notification may confirm delivery before publication."""

    def retain_before(self, later: set[tuple[str, str]]) -> bool:
        """Whether this update still needs publication before later updates."""
        return True

    def page_record(self, turn_id: str, item_id: str) -> FinishedItem:
        """Even an invisible item owns a source position and its page continuation."""
        return FinishedItem(turn_id, item_id, ())

    def add_to(self, history: ImportBuffer) -> None:
        """What this update contributes to an imported conversation; activity contributes nothing."""

    @staticmethod
    def coalesced(updates: list[TranscriptUpdate]) -> list[TranscriptUpdate]:
        later: set[tuple[str, str]] = set()
        kept = [update for update in reversed(updates) if update.retain_before(later)]
        return list(reversed(kept))


class StatusUpdate(TranscriptUpdate):
    """Reader phases decide when current activity is published."""

    def during(self, phase: ClaudeReaderPhase) -> tuple[TranscriptUpdate, ...]:
        return phase.status(self)


@dataclass(frozen=True)
class TurnStarted(StatusUpdate):
    detail: str = ''

    def publish(self, output: ThreadOutput) -> None:
        output.status(ActivityState.WORKING, self.detail)


@dataclass(frozen=True)
class TurnEnded(StatusUpdate):
    def publish(self, output: ThreadOutput) -> None:
        output.transcript.end_turn()
        output.status(ActivityState.IDLE)


@dataclass(frozen=True)
class ActiveItem(StatusUpdate):
    """An item the CLI started producing: what it is doing now."""
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

    def page_record(self, turn_id: str, item_id: str) -> FinishedItem:
        return self

    def add_to(self, history: ImportBuffer) -> None:
        history.add(self.events)


@dataclass(frozen=True)
class PageItem:
    """An item-list identity survives even when its update is invisible in the live view."""
    turn_id: str
    item_id: str
    update: TranscriptUpdate


@dataclass(frozen=True)
class PublishedPage(TranscriptUpdate):
    """A bounded harness snapshot publishes items, never current activity or drafts."""
    items: tuple[PageItem, ...]
    earlier: ExternalPage

    def publish(self, output: ThreadOutput) -> None:
        records = tuple(item.update.page_record(item.turn_id, item.item_id) for item in self.items)
        self.write(output.transcript, records)

    def write(self, transcript: ExternalTranscript, records: tuple[FinishedItem, ...]) -> None:
        transcript.publish_page(records, self.earlier)

    def add_to(self, history: ImportBuffer) -> None:
        for item in self.items:
            item.update.add_to(history)


@dataclass(frozen=True)
class EarlierPublishedPage(PublishedPage):
    request: EarlierItems

    def write(self, transcript: ExternalTranscript, records: tuple[FinishedItem, ...]) -> None:
        transcript.prepend(self.request, records, self.earlier)


@dataclass(frozen=True)
class HiddenItem(TranscriptUpdate):
    """Conversation the live view does not show, because the thread shows it another way
    (a bus delivery) or because only the person's own prompts are shown; history keeps it."""
    events: tuple[TranscriptEvent, ...]

    def publish(self, output: ThreadOutput) -> None:
        pass

    def add_to(self, history: ImportBuffer) -> None:
        history.add(self.events)


@dataclass(frozen=True)
class Summarized(TranscriptUpdate):
    """The harness replaced everything before this point with a summary (compaction)."""
    text: str

    def publish(self, output: ThreadOutput) -> None:
        pass

    def add_to(self, history: ImportBuffer) -> None:
        history.summarize(self.text)


@dataclass(frozen=True)
class SessionFacts(TranscriptUpdate):
    """Which session this is, where it ran and its title; a later fact replaces an earlier one."""
    source_id: str = ''
    project: str = ''
    title: str = ''

    def publish(self, output: ThreadOutput) -> None:
        pass

    def add_to(self, history: ImportBuffer) -> None:
        history.describe(self)


@dataclass(frozen=True)
class EarlierRequest(TranscriptUpdate):
    """The newest request before the selected history, looked up only if that history has none."""
    find: Callable[[], str] = field(compare=False)

    def publish(self, output: ThreadOutput) -> None:
        pass

    def add_to(self, history: ImportBuffer) -> None:
        if not history.latest_request:
            history.latest_request = self.find()[: history.limits.per_message]


@dataclass(frozen=True)
class HistoryNotice(TranscriptUpdate):
    """Something about the source that an imported conversation should say it left out."""
    text: str

    def publish(self, output: ThreadOutput) -> None:
        pass

    def add_to(self, history: ImportBuffer) -> None:
        history.notices.add(self.text)


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
        for operation in bridge_type.operations:
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
        await bridge.show_sent(result, self.target, self.body)


class Inbox(Operation[OperationBridge]):
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
            from agent_comms.registration import Registration

            registry = await asyncio.to_thread(
                Registration, Path(bridge.binding.root) / 'registry.json')
            # The endpoint, not a particular CLI's record parser, owns presence.
            with registry.external_process(bridge.binding.bus, ProcessIdentity.capture(os.getpid())):
                async with asyncio.TaskGroup() as tasks:
                    self.start(bridge, tasks)


class Watch(Inbox):
    def start(self, bridge: TranscriptBridge, tasks: asyncio.TaskGroup) -> None:
        super().start(bridge, tasks)
        tasks.create_task(bridge.publish_transcript())


@dataclass(frozen=True)
class IncomingFrame(ABC):
    message: Message

    @property
    def text(self) -> str:
        origin = self.message.sender_role.origin_label
        return (f'Comms in · {self.message.sender} ({origin})\n'
                f'{self.message.body}\nMessage ID: {self.message.message_id}')

class CliBridge(ABC, Generic[BridgeOptions]):
    """Core delivery and lifetime shared by CLI-specific transports."""

    operations: ClassVar[tuple[type[Operation], ...]] = (Inbox, Send)

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
    async def deliver(self, message: Message) -> type[Outcome]:
        """Return harness evidence; the shared watch owns completion."""

    @abstractmethod
    async def show_sent(self, result: str, target: str, body: str) -> None:
        """Display a completed send without publishing it again."""

    @abstractmethod
    async def report_failure(self, text: str) -> None:
        """Report a bridge failure through the CLI's visible output."""

    def sent_text(self, result: str, target: str, body: str) -> str:
        return (f'Comms out · {self.binding.bus} → {target}\n'
                f'{body}\nMessage ID: {result}')

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
        while True:
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
            except MaintenanceHeldError:
                print(f'Bus maintenance: {self.binding.bus} paused until it clears',
                      file=sys.stderr, flush=True)
            finally:
                await child.stop()
            await self.comms.bus.resumed()

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


class TranscriptBridge(CliBridge[BridgeOptions]):
    """A CLI with an event subscription also publishes whole transcript items."""

    operations = (*CliBridge.operations, Watch)

    @abstractmethod
    def transcript(self) -> AsyncIterator[TranscriptUpdate]:
        """Observe CLI output without submitting inputs or executing tools."""

    async def publish_transcript(self) -> None:
        comms = self.comms
        output = ThreadOutput(
            await asyncio.to_thread(comms.transcripts.external, self.binding.bus), comms.agents)
        # This subscription can assemble only items whose start it receives.
        # A prefix from the previous subscription cannot stand for that stream.
        await asyncio.to_thread(output.transcript.end_turn)
        # Each CLI reports its own initial state. Disk writes never stop the
        # event reader from receiving a newer whole draft of the same item.
        queue: asyncio.Queue[TranscriptUpdate] = asyncio.Queue()

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
                await asyncio.to_thread(output.publish_batch, pending)

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
    incoming_frame: ClassVar[type[IncomingFrame]]
    window_bytes: ClassVar[int]

    def __init__(self, options: Options, binding: Binding, db: CoordinationStore) -> None:
        super().__init__(options, binding, db)
        self.probe = MonitorProbe(session=binding.thread, token=secrets.token_hex(24))
        self.monitor_ready = asyncio.Event()
        self.pending_parts: dict[tuple[str, int], int] = {}
        self.window_changed = asyncio.Condition()
        self.write_line = ExplicitWatchAcknowledgment.line_output(sys.stdout)

    async def prepare_delivery(self) -> None:
        self.write_line(self.probe.model_dump_json())
        print('Waiting for this session to observe the monitor probe; inbox remains unread.',
              file=sys.stderr, flush=True)
        # The harness owns notification scheduling. Missing proof never opens
        # the inbox; the watch's existing cancellation owns this wait's lifetime.
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

    async def deliver(self, message: Message) -> type[Outcome]:
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

    async def show_sent(self, result: str, target: str, body: str) -> None:
        self.write_line(self.encode(self.sent_text(result, target, body)))

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
