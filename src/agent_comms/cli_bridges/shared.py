"""Shared Core inbox delivery and process lifetime for external CLI adapters."""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import signal
import sqlite3
import sys
from abc import ABC, abstractmethod
from contextlib import aclosing, suppress
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import AsyncIterator, ClassVar, Generic, TypeVar

from pydantic import JsonValue, TypeAdapter

from agent_comms.activity import ActivityState
from agent_comms.agent_activity import AgentActivity
from agent_comms.child_process import ProcessIdentity
from agent_comms.cli_protocol import CORE_ERROR, CoreError, WireModel
from agent_comms.external_transcript import ExternalTranscript
from agent_comms.messages import Message, MessageKind
from agent_comms.thread_identity import ThreadRole
from agent_comms.tool_declaration import ToolDeclaration
from agent_comms.tool_results import ToolDiff
from agent_comms.transcript_events import TextTranscript, ToolEndTranscript, ToolStartTranscript, TranscriptEvent


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
class Binding:
    root: str
    bus: str
    thread: str


class DraftAccumulator(ABC):
    """One subscription's item prefix; the shared output owns its lifetime."""

    @abstractmethod
    def started_at(self) -> float | None: ...

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


@dataclass
class NonTextDraft(DraftAccumulator):
    timestamp: float | None

    def started_at(self) -> float | None:
        return self.timestamp


@dataclass
class TextDraft(DraftAccumulator):
    template: TextTranscript
    sections: dict[int, list[str]]
    separator: ClassVar[str] = ''

    @classmethod
    def from_event(cls, event: TextTranscript) -> TextDraft:
        return cls.from_parts(event, (event.text,))

    @classmethod
    def from_parts(cls, template: TextTranscript, parts: Iterable[str]) -> TextDraft:
        return cls(replace(template, text=''), {index: [text] for index, text in enumerate(parts)})

    def started_at(self) -> float | None:
        return self.template.timestamp

    def append(self, text: str, section: int) -> None:
        self.sections.setdefault(section, []).append(text)

    @property
    def events(self) -> tuple[TranscriptEvent, ...]:
        text = self.separator.join(text for index in sorted(self.sections)
                                   if (text := ''.join(self.sections[index])))
        return (replace(self.template, text=text),) if text else ()

    def snapshot(self, turn_id: str, item_id: str) -> tuple[DraftItem, ...]:
        return (DraftItem(turn_id, item_id, self),)


class ParagraphDraft(TextDraft):
    separator = '\n\n'


class ClosedDraft(NonTextDraft):
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

    def start_item(self, update: StartedItem) -> tuple[TranscriptUpdate, ...]:
        key = update.turn_id, update.item_id
        previous = self.drafts.get(key)
        current = update.accumulator if previous is None else previous.restarted(update.accumulator)
        self.drafts[key] = current
        return current.started(update)

    def active_item(self, update: ActiveItem) -> tuple[TranscriptUpdate, ...]:
        current = self.drafts.get((update.turn_id, update.item_id))
        return (update,) if current is None else current.activity(update)

    def append_item(self, update: AppendDraftItem) -> tuple[TranscriptUpdate, ...]:
        current = self.drafts.get((update.turn_id, update.item_id))
        if current is None:
            # No observed prefix: a suffix cannot reconstruct the whole item.
            return ()
        current.append(update.text, update.section)
        return current.snapshot(update.turn_id, update.item_id)

    def finish_item(self, update: CompletedItem) -> FinishedItem:
        key = update.turn_id, update.item_id
        current = self.drafts.get(key)
        timestamp = current.started_at() if current is not None else None
        self.drafts[key] = ClosedDraft(timestamp)
        events = update.events if timestamp is None else tuple(
            replace(event, timestamp=timestamp) for event in update.events)
        return FinishedItem(update.turn_id, update.item_id, events)

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

class Outcome(Enum):
    FILTERED = 'filtered'
    UNCONFIRMED = 'unconfirmed'
    DELIVERED = 'delivered'
    EMITTED = 'emitted'

class DeliveryJournal:
    """Transport outcomes only. Core still owns messages and inbox routing."""

    def __init__(self, state: Path) -> None:
        self.path = state / 'delivery.sqlite3'
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS binding (root TEXT, bus TEXT, thread TEXT)')
        self.db.execute('''CREATE TABLE IF NOT EXISTS delivery (
            id TEXT PRIMARY KEY, payload TEXT NOT NULL, outcome TEXT NOT NULL)''')
        self.db.commit()

    def bind(self, requested: Binding | None) -> Binding:
        rows = self.db.execute('SELECT root,bus,thread FROM binding').fetchall()
        if not rows and requested is not None:
            with self.db:
                self.db.execute('INSERT INTO binding VALUES (?,?,?)',
                                (requested.root, requested.bus, requested.thread))
            return requested
        if len(rows) != 1:
            raise ValueError('Supply one complete Core/CLI binding for this state directory')
        current = Binding(*rows[0])
        if requested is not None and requested != current:
            raise ValueError('This state directory belongs to another Core/CLI conversation')
        return current

    def begin(self, message: Message, payload: str, outcome: Outcome) -> bool:
        with self.db:
            return self.db.execute('INSERT OR IGNORE INTO delivery VALUES (?,?,?)',
                                   (message.message_id, payload, outcome.value)).rowcount == 1

    def finish(self, message: Message, outcome: Outcome) -> None:
        with self.db:
            self.db.execute('UPDATE delivery SET outcome=? WHERE id=?',
                            (outcome.value, message.message_id))

    def report_unconfirmed(self) -> None:
        rows = self.db.execute('SELECT id FROM delivery WHERE outcome=?',
                               (Outcome.UNCONFIRMED.value,)).fetchall()
        if rows:
            print(f'Unconfirmed messages retained without replay: {[row[0] for row in rows]}',
                  file=sys.stderr, flush=True)

    def close(self) -> None:
        self.db.close()

class CoreConnection:
    """Core's CLI owns publication and the watch stream, including its codec."""

    def __init__(self, executable: str, binding: Binding) -> None:
        self.executable = executable
        self.binding = binding

    async def send(self, target: str, body: str, kind: str) -> SendResult:
        child = await asyncio.create_subprocess_exec(
            self.executable, '--root', self.binding.root, 'send', '--from', self.binding.bus,
            '--to', target, '--type', kind, '--body', body,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            async with asyncio.timeout(60):
                stdout, stderr = await child.communicate()
        finally:
            await self.join(child)
        # Publication and display are separate facts. Keep Core's result even
        # when displaying it fails; the caller must never repeat the send.
        print(stdout.decode(), end='', flush=True)
        print(stderr.decode(), end='', file=sys.stderr, flush=True)
        result = SEND_RESULT.validate_json(stdout)
        if isinstance(result, CoreError):
            result.raise_failure('send')
        if child.returncode:
            raise RuntimeError(f'Core send exited {child.returncode}; inspect the printed result')
        return result

    async def watch(self, state: Path) -> AsyncIterator[tuple[Message, str] | CoreError]:
        with (state / 'watch.stderr.log').open('a') as errors:
            child = await asyncio.create_subprocess_exec(
                self.executable, '--root', self.binding.root, 'watch', '--thread', self.binding.bus,
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

SEND_RESULT = TypeAdapter(SendResult | CoreError.declared_union())

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
    kind: str

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        parser.add_argument('--to', required=True)
        parser.add_argument('--body', required=True)
        parser.add_argument('--type', default='info', choices=MessageKind.sendable_names())

    @classmethod
    def from_arguments(cls, arguments: argparse.Namespace) -> Send:
        send = cls(arguments.to, arguments.body, arguments.type)
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
        with (bridge.options.state / 'watch.lock').open('a') as lock:
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


class CliBridge(ABC, Generic[BridgeOptions]):
    """Core delivery and lifetime shared by CLI-specific transports."""

    operations: ClassVar[tuple[type[Operation], ...]] = (Inbox, Send)

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        """Declare the options this harness's transport needs."""

    def __init__(self, options: BridgeOptions, binding: Binding, journal: DeliveryJournal) -> None:
        self.options = options
        self.binding = binding
        self.journal = journal
        # The CLI of the runtime this bridge runs from.
        self.core = CoreConnection(str(Path(sys.executable).with_name('agent-comms')), binding)

    @abstractmethod
    async def deliver(self, message: Message) -> Outcome:
        """Return the actual transport result: acknowledgment or stdout emission."""

    @abstractmethod
    async def show_sent(self, result: SendResult, target: str, body: str) -> None:
        """Display a completed send without publishing it again."""

    @abstractmethod
    async def report_failure(self, text: str) -> None:
        """Report a bridge failure through the CLI's visible output."""

    @staticmethod
    def incoming_text(message: Message) -> str:
        origin = 'human' if message.sender_role is ThreadRole.USER else 'external agent'
        return (f'Comms in · {message.sender} ({origin})\n'
                f'{message.body}\nMessage ID: {message.message_id}')

    def sent_text(self, result: SendResult, target: str, body: str) -> str:
        return (f'Comms out · {self.binding.bus} → {target}\n'
                f'{body}\nMessage ID: {result.id}')

    async def watch(self) -> None:
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
                    outcome = Outcome.UNCONFIRMED if conversation else Outcome.FILTERED
                    if not self.journal.begin(message, payload, outcome) or not conversation:
                        continue
                    result = await self.deliver(message)
                    self.journal.finish(message, result)
                    print(f'{result.value}: {message.message_id} from {message.sender}',
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
        from agent_comms.comms import Comms

        comms = Comms(Path(self.binding.root))
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

class LineMonitorBridge(CliBridge[Options]):
    """Harnesses whose monitor injects each stdout line as a notification."""

    @staticmethod
    def emit(text: str) -> None:
        # One reversible JSON string per line; no truncation or lost line breaks.
        print(json.dumps(text, ensure_ascii=False), flush=True)

    async def deliver(self, message: Message) -> Outcome:
        self.emit(self.incoming_text(message))
        return Outcome.EMITTED  # A pipe write is not a harness acknowledgment.

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
