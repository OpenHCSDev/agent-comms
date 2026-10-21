"""OpenCode sessions held by a running OpenCode server, and the bridge that follows one."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import os
from pathlib import Path
from typing import AsyncIterator, ClassVar, Self
from urllib.parse import quote

import aiohttp
from agent_comms.coordination_database import CoordinationStore
from agent_comms.child_process import ProcessIdentity
from agent_comms.messages import Message
from agent_comms.send_result import SendResult
from agent_comms.cli_protocol import Outcome, DeliveredOutcome

from .history import HarnessAddress, OnlineHistory, OpenCodeHistory
from .shared import Binding, CliBridge, IncomingFrame, Options, TranscriptBridge, Watch, run_bridge
from agent_comms.runtime_updates import RuntimeUpdate
from .opencode_protocol import (
    Connected, OpenCodeEvent, OpenCodeStream, Prompt, SavedInput, Session, TextInput, Toast,
    SESSION_STATUSES, MESSAGES, IdleStatus,
)
from .opencode_parts import SavedMessage, UserMessage


async def event_payloads(response: aiohttp.ClientResponse) -> AsyncIterator[str]:
    """Decode SSE framing across arbitrary HTTP chunks, without a line-size cap."""
    pending = b''
    data: list[bytes] = []
    async for chunk in response.content.iter_any():
        pending += chunk
        while b'\n' in pending:
            line, pending = pending.split(b'\n', 1)
            line = line.removesuffix(b'\r')
            if not line:
                if data:
                    yield b'\n'.join(data).decode('utf-8')
                    data.clear()
                continue
            field, separator, value = line.partition(b':')
            if field == b'data':
                data.append(value.removeprefix(b' ') if separator else b'')
    raise ConnectionError('OpenCode event stream ended')


@dataclass
class OpenCodeOptions(Options):
    server: str
    directory: Path


@dataclass(frozen=True)
class Attach(Watch):
    """The selected native session owns its Core thread and bridge child."""

    pid: int

    @classmethod
    def serves(cls, bridge: type[CliBridge]) -> bool:
        return issubclass(bridge, OpenCodeBridge)

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        parser.add_argument('--pid', type=int, required=True)

    def binding(self, arguments: argparse.Namespace) -> Binding:
        from agent_comms.comms import wire

        if arguments.session is None:
            raise ValueError('Attach requires the selected native session')
        comms = wire(arguments.root)
        name = arguments.bus_thread or f'opencode-{arguments.session}'
        arguments.state = arguments.state / arguments.session
        return Binding(str(comms.root), name, arguments.session)

    def start(self, bridge: OpenCodeBridge, tasks: asyncio.TaskGroup) -> None:
        from agent_comms.comms import wire
        from agent_comms.cli_commands import RegisterCliCommand

        RegisterCliCommand(name=bridge.binding.bus, worktree=str(bridge.options.directory),
                           process=ProcessIdentity.capture(self.pid)).apply(wire(bridge.binding.root))
        super().start(bridge, tasks)


class OpenCodeConnection:
    """HTTP input and its persistence confirmation share the same event stream."""

    def __init__(self, http: aiohttp.ClientSession, session: Session) -> None:
        self.http = http
        self.info = session
        self.session = session.id
        self.path = '/session/' + quote(session.id, safe='')

    @classmethod
    @asynccontextmanager
    async def open(cls, server: str, directory: Path, session: str) -> AsyncIterator[OpenCodeConnection]:
        password = os.environ.get('OPENCODE_SERVER_PASSWORD')
        auth = (aiohttp.BasicAuth(os.environ.get('OPENCODE_SERVER_USERNAME', 'opencode'), password)
                if password is not None else None)
        async with aiohttp.ClientSession(
            base_url=server.rstrip('/'), auth=auth,
            headers={'x-opencode-directory': str(directory.resolve())},
            timeout=aiohttp.ClientTimeout(total=30), raise_for_status=True,
        ) as http:
            async with http.get('/session/' + quote(session, safe='')) as response:
                actual = Session.model_validate_json(await response.read())
            if actual.id != session or Path(actual.directory).resolve() != directory.resolve():
                raise ValueError('OpenCode returned a different session or working directory')
            yield cls(http, actual)

    async def saved_input(self, message_id: str) -> UserMessage:
        async with self.http.get(self.path + '/message/' + quote(message_id, safe='')) as response:
            saved = SavedInput.model_validate_json(await response.read())
        if saved.info.id != message_id or saved.info.sessionID != self.session:
            raise ValueError('OpenCode returned a different saved message')
        return saved.info

    async def message(self, message_id: str) -> SavedMessage:
        async with self.http.get(self.path + '/message/' + quote(message_id, safe='')) as response:
            saved = SavedMessage.model_validate_json(await response.read())
        if saved.info.id != message_id or saved.info.sessionID != self.session:
            raise ValueError('OpenCode returned a different saved message')
        return saved

    async def messages(self, query: dict[str, str]) -> list[SavedMessage]:
        async with self.http.get(self.path + '/message', params=query) as history:
            return MESSAGES.validate_json(await history.read())

    async def status(self) -> RuntimeUpdate:
        async with self.http.get('/session/status') as status_response:
            statuses = SESSION_STATUSES.validate_json(await status_response.read())
        # OpenCode omits idle sessions from its status map.
        return (statuses[self.session] if self.session in statuses else IdleStatus()).publication()

    async def deliver(self, text: str) -> UserMessage:
        async with asyncio.timeout(30):
            async with self.http.get('/event') as response:
                payloads = event_payloads(response)
                # OpenCode emits this after subscribing, before any prompt is sent.
                Connected.model_validate_json(await anext(payloads))
                prompt = Prompt(parts=(TextInput(text=text),))
                async with self.http.post(
                    self.path + '/prompt_async', data=prompt.model_dump_json(),
                    headers={'Content-Type': 'application/json'},
                ) as submitted:
                    await submitted.read()
                async for payload in payloads:
                    saved = await OpenCodeEvent.decode(payload).saved_input(self, text)
                    if saved is not None:
                        return saved
        raise ConnectionError('OpenCode ended input confirmation without a saved message')

    async def toast(self, toast: Toast) -> None:
        async with self.http.post(
            '/tui/show-toast', data=toast.model_dump_json(),
            headers={'Content-Type': 'application/json'},
        ) as response:
            await response.read()


class OpenCodeServer(OpenCodeHistory, OnlineHistory):
    """A session the running OpenCode server holds, read through its message list."""

    query: ClassVar[dict[str, str]] = {}

    def __init__(self, session: str, server: str, directory: Path) -> None:
        self.session = session
        self.server = server
        self.directory = directory

    @classmethod
    def connected(cls, thread: str, address: HarnessAddress, directory: Path) -> Self:
        return cls(
            thread, address.require('Reading a running OpenCode session needs its server address.'),
            directory)

    def connection(self) -> AsyncIterator[OpenCodeConnection]:
        return OpenCodeConnection.open(self.server, self.directory, self.session)

    async def history(self) -> AsyncIterator[RuntimeUpdate]:
        async with self.connection() as connection:
            async for update in self.seen(connection, OpenCodeStream(connection)):
                yield update

    async def seen(self, connection: OpenCodeConnection, stream: OpenCodeStream) -> AsyncIterator[RuntimeUpdate]:
        messages = await connection.messages(self.query)
        stream.know(messages)
        for update in self.saved(connection.info, messages):
            yield update
        yield await connection.status()


class OpenCodeBridge(OpenCodeServer, TranscriptBridge[OpenCodeOptions], declared_name=None):
    query = {'limit': '20'}  # A restarted bridge publishes again only the newest messages.

    def __init__(self, options: OpenCodeOptions, binding: Binding, db: CoordinationStore) -> None:
        TranscriptBridge.__init__(self, options, binding, db)
        OpenCodeServer.__init__(self, binding.thread, options.server, options.directory)

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        parser.add_argument('--server', required=True, help='HTTP address of the running OpenCode TUI/server')
        parser.add_argument('--directory', type=Path, default=Path.cwd(), help='OpenCode session working directory')

    async def transcript(self) -> AsyncIterator[RuntimeUpdate]:
        async with self.connection() as connection:
            # Subscribe before taking snapshots; the HTTP stream retains the overlap.
            # Finished identities prevent buffered parts reopening a finished draft.
            async with connection.http.get('/event', timeout=aiohttp.ClientTimeout(total=None)) as response:
                payloads = event_payloads(response)
                Connected.model_validate_json(await anext(payloads))
                stream = OpenCodeStream(connection)
                async for update in self.seen(connection, stream):
                    yield update
                async for payload in payloads:
                    for update in await OpenCodeEvent.decode(payload).receive(stream):
                        yield update

    async def deliver(self, message: Message) -> type[Outcome]:
        async with self.connection() as connection:
            await connection.deliver(IncomingFrame(message).text)
        return DeliveredOutcome

    async def show_sent(self, result: SendResult) -> None:
        async with self.connection() as connection:
            await connection.toast(Toast(message=self.sent_text(result), variant='success'))

    async def report_failure(self, text: str) -> None:
        async with self.connection() as connection:
            await connection.toast(Toast(message=text, variant='error'))


def main() -> None:
    run_bridge(OpenCodeBridge, OpenCodeOptions.parse(__doc__, Path.home() / '.local/state/opencode-comms-inbox', OpenCodeBridge))

