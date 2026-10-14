"""Shared Core inbox delivery and process lifetime for external CLI adapters."""
from __future__ import annotations

from abc import ABC, abstractmethod
import argparse
import asyncio
from dataclasses import dataclass
from enum import Enum
import fcntl
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
from typing import AsyncIterator, ClassVar, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, TypeAdapter
from metaclass_registry import AutoRegisterMeta
from agent_comms.messages import Message, MessageKind
from agent_comms.thread_identity import ThreadRole
from agent_comms.transcript_events import TranscriptEvent


class WireModel(BaseModel):
    """Strict fields at an external protocol boundary; unused metadata stays outside."""
    model_config = ConfigDict(strict=True)

class RegisteredWireMeta(AutoRegisterMeta, type(WireModel)):
    """Compose declaration registration with external-field validation."""

class CoreError(WireModel):
    error: str

class SendResult(WireModel):
    id: str

class WatchDatabaseBusy(RuntimeError):
    """Only a failed Core read stream may be reconnected automatically."""

@dataclass(frozen=True)
class Binding:
    root: str
    bus: str
    thread: str


@dataclass(frozen=True)
class TranscriptUpdate:
    """One CLI item's complete current presentation, identified by its producer."""
    turn_id: str
    item_id: str
    events: tuple[TranscriptEvent, ...]

@dataclass
class Options:
    operation: Operation
    root: str | None
    bus_thread: str | None
    session: str | None
    state: Path
    comms: str

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
            raise RuntimeError(f'Core send: {result.error}')
        if child.returncode:
            raise RuntimeError(f'Core send exited {child.returncode}; inspect the printed result')
        return result

    async def watch(self, state: Path) -> AsyncIterator[tuple[Message, str]]:
        with (state / 'watch.stderr.log').open('a') as errors:
            child = await asyncio.create_subprocess_exec(
                self.executable, '--root', self.binding.root, 'watch', '--thread', self.binding.bus,
                stdout=asyncio.subprocess.PIPE, stderr=errors)
            assert child.stdout is not None
            try:
                while line := await child.stdout.readline():
                    try:
                        wire = json.loads(line)
                    except json.JSONDecodeError:
                        # The CLI emits one multiline JSON error before exiting.
                        async with asyncio.timeout(5):
                            line += await child.stdout.read()
                            await child.wait()
                        (state / 'watch-failure.txt').write_bytes(line)
                        wire = json.loads(line)
                    if isinstance(wire, dict) and 'error' in wire:
                        error = CoreError.model_validate(wire)
                        if error.error == 'database is locked':
                            raise WatchDatabaseBusy(error.error)
                        raise RuntimeError(f'Core watch: {error.error}')
                    yield Message.from_wire(wire), line.decode()
                raise RuntimeError(f'Core watch ended, exit {await child.wait()}')
            finally:
                await self.join(child)

    @staticmethod
    async def join(child: asyncio.subprocess.Process) -> None:
        if child.returncode is None:
            child.terminate()
            try:
                async with asyncio.timeout(5):
                    await child.wait()
            except TimeoutError:
                child.kill()
                await child.wait()

SEND_RESULT = TypeAdapter(SendResult | CoreError)

BridgeOptions = TypeVar('BridgeOptions', bound=Options)
OperationBridge = TypeVar('OperationBridge', bound='CliBridge')


class Operation(ABC, Generic[OperationBridge]):
    """A decoded CLI command owns its execution."""

    @classmethod
    @abstractmethod
    def from_arguments(cls, parser: argparse.ArgumentParser,
                       arguments: argparse.Namespace) -> Operation: ...

    @abstractmethod
    async def run(self, bridge: OperationBridge) -> None: ...


@dataclass(frozen=True)
class Send(Operation['CliBridge']):
    target: str
    body: str
    kind: str

    @classmethod
    def from_arguments(cls, parser: argparse.ArgumentParser,
                       arguments: argparse.Namespace) -> Send:
        if arguments.to is None or arguments.body is None:
            parser.error('send requires --to and --body')
        return cls(arguments.to, arguments.body, arguments.type)

    async def run(self, bridge: CliBridge) -> None:
        result = await bridge.core.send(self.target, self.body, self.kind)
        await bridge.show_sent(result, self.target, self.body)


class Inbox(Operation[OperationBridge]):
    @classmethod
    def from_arguments(cls, parser: argparse.ArgumentParser,
                       arguments: argparse.Namespace) -> Inbox:
        if arguments.to is not None or arguments.body is not None:
            parser.error('--to and --body belong to send')
        return cls()

    def start(self, bridge: OperationBridge, tasks: asyncio.TaskGroup) -> None:
        tasks.create_task(bridge.watch())

    async def run(self, bridge: OperationBridge) -> None:
        with (bridge.options.state / 'watch.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                async with asyncio.TaskGroup() as tasks:
                    self.start(bridge, tasks)
            except ExceptionGroup as error:
                # Join all tasks, retaining the original single failure's exit policy.
                if len(error.exceptions) == 1:
                    raise error.exceptions[0] from error
                raise


class Watch(Inbox['TranscriptBridge']):
    def start(self, bridge: TranscriptBridge, tasks: asyncio.TaskGroup) -> None:
        super().start(bridge, tasks)
        tasks.create_task(bridge.publish_transcript())


class CliBridge(ABC, Generic[BridgeOptions]):
    """Core delivery and lifetime shared by CLI-specific transports."""

    operations: ClassVar[tuple[type[Operation], ...]] = (Inbox, Send)

    def __init__(self, options: BridgeOptions, binding: Binding, journal: DeliveryJournal) -> None:
        self.options = options
        self.binding = binding
        self.journal = journal
        self.core = CoreConnection(options.comms, binding)

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
        async for message, payload in self.core.watch(self.options.state):
            filtered = not message.type.conversation_message
            outcome = Outcome.FILTERED if filtered else Outcome.UNCONFIRMED
            if not self.journal.begin(message, payload, outcome) or filtered:
                continue
            result = await self.deliver(message)
            self.journal.finish(message, result)
            print(f'{result.value}: {message.message_id} from {message.sender}',
                  file=sys.stderr, flush=True)

    async def run(self) -> None:
        try:
            await self.options.operation.run(self)
        except (WatchDatabaseBusy, BlockingIOError, asyncio.CancelledError):
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

        transcripts = Comms(Path(self.binding.root)).transcripts
        identity = await asyncio.to_thread(
            transcripts.bind_external, self.binding.bus, self.binding.thread)
        async for update in self.transcript():
            await asyncio.to_thread(
                transcripts.publish_external, identity, turn_id=update.turn_id,
                item_id=update.item_id, events=update.events)

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
    parser = argparse.ArgumentParser(description=description)
    operations = {operation.__name__.lower(): operation for operation in bridge_type.operations}

    def decode_operation(value: str) -> type[Operation]:
        try:
            return operations[value]
        except KeyError:
            raise argparse.ArgumentTypeError(f'Choose one of: {", ".join(operations)}') from None

    parser.add_argument('operation', nargs='?', type=decode_operation,
                        metavar='{' + ','.join(operations) + '}',
                        default=bridge_type.operations[0])
    parser.add_argument('--root')
    parser.add_argument('--bus-thread')
    parser.add_argument('--session')
    parser.add_argument('--state', type=Path, default=state)
    parser.add_argument('--to')
    parser.add_argument('--body')
    parser.add_argument('--type', default='info', choices=MessageKind.sendable_names())
    parser.add_argument('--comms', default=str(Path.home() / '.local/bin/agent-comms'))
    return parser


def parse_options(parser: argparse.ArgumentParser,
                  options_type: type[BridgeOptions]) -> BridgeOptions:
    arguments = parser.parse_args()
    arguments.operation = arguments.operation.from_arguments(parser, arguments)
    del arguments.to, arguments.body, arguments.type
    return options_type(**vars(arguments))


def run_bridge(bridge_type: type[CliBridge[BridgeOptions]], options: BridgeOptions) -> None:
    requested = None
    if any((options.root, options.bus_thread, options.session)):
        if not all((options.root, options.bus_thread, options.session)):
            raise ValueError('Supply all three binding arguments or use the saved binding')
        requested = Binding(options.root, options.bus_thread, options.session)
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
    except WatchDatabaseBusy as error:
        print(f'Core watch busy: {error}; reconnecting read stream', file=sys.stderr)
        sys.exit(75)
    except BlockingIOError:
        sys.exit('Another watcher owns this conversation')
    finally:
        journal.close()
