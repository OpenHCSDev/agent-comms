"""Connect a running OpenCode HTTP session to the shared Core CLI bridge."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import os
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import quote

import aiohttp
from agent_comms.messages import Message

from .shared import (
    TranscriptBridge, TranscriptUpdate, Options, Outcome, DeliveredOutcome, SendResult, parse_options, run_bridge, shared_parser,
    Watch, Binding,
)
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
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        parser.add_argument('--pid', type=int, required=True)

    @classmethod
    def from_arguments(cls, arguments: argparse.Namespace) -> Attach:
        operation = cls(arguments.pid)
        del arguments.pid
        return operation

    def binding(self, options: OpenCodeOptions) -> Binding:
        from agent_comms.comms import wire

        if options.session is None:
            raise ValueError('Attach requires the selected native session')
        comms = wire(options.root)
        name = options.bus_thread or f'opencode-{options.session}'
        options.state = options.state / options.session
        return Binding(str(comms.root), name, options.session)

    def start(self, bridge: OpenCodeBridge, tasks: asyncio.TaskGroup) -> None:
        from agent_comms.comms import wire
        from agent_comms.cli_commands import RegisterCliCommand

        RegisterCliCommand(name=bridge.binding.bus, worktree=str(bridge.options.directory),
                           pid=self.pid).apply(wire(bridge.binding.root))
        super().start(bridge, tasks)


class OpenCodeConnection:
    """HTTP input and its persistence confirmation share the same event stream."""

    def __init__(self, http: aiohttp.ClientSession, session: str) -> None:
        self.http = http
        self.session = session
        self.path = '/session/' + quote(session, safe='')

    @classmethod
    @asynccontextmanager
    async def open(cls, options: OpenCodeOptions, session: str) -> AsyncIterator[OpenCodeConnection]:
        password = os.environ.get('OPENCODE_SERVER_PASSWORD')
        auth = (aiohttp.BasicAuth(os.environ.get('OPENCODE_SERVER_USERNAME', 'opencode'), password)
                if password is not None else None)
        async with aiohttp.ClientSession(
            base_url=options.server.rstrip('/'), auth=auth,
            headers={'x-opencode-directory': str(options.directory.resolve())},
            timeout=aiohttp.ClientTimeout(total=30), raise_for_status=True,
        ) as http:
            connection = cls(http, session)
            async with http.get(connection.path) as response:
                actual = Session.model_validate_json(await response.read())
            if actual.id != session or Path(actual.directory).resolve() != options.directory.resolve():
                raise ValueError('OpenCode returned a different session or working directory')
            yield connection

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

    async def transcript(self) -> AsyncIterator[TranscriptUpdate]:
        # Subscribe before taking snapshots; the HTTP stream retains the overlap.
        # Finished identities prevent buffered parts reopening a finished draft.
        async with self.http.get('/event', timeout=aiohttp.ClientTimeout(total=None)) as response:
            payloads = event_payloads(response)
            Connected.model_validate_json(await anext(payloads))
            stream = OpenCodeStream(self)
            async with self.http.get(self.path + '/message', params={'limit': '20'}) as history:
                messages = MESSAGES.validate_json(await history.read())
            for saved in messages:
                for update in stream.snapshot(saved):
                    yield update
            async with self.http.get('/session/status') as status_response:
                statuses = SESSION_STATUSES.validate_json(await status_response.read())
            # OpenCode omits idle sessions from its status map.
            yield (statuses[self.session] if self.session in statuses else IdleStatus()).publication()
            async for payload in payloads:
                for update in await OpenCodeEvent.decode(payload).receive(stream):
                    yield update

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


class OpenCodeBridge(TranscriptBridge[OpenCodeOptions]):
    operations = (*TranscriptBridge.operations, Attach)

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        parser.add_argument('--server', required=True, help='HTTP address of the running OpenCode TUI/server')
        parser.add_argument('--directory', type=Path, default=Path.cwd(), help='OpenCode session working directory')

    async def transcript(self) -> AsyncIterator[TranscriptUpdate]:
        async with OpenCodeConnection.open(self.options, self.binding.thread) as connection:
            async for update in connection.transcript():
                yield update

    async def deliver(self, message: Message) -> type[Outcome]:
        async with OpenCodeConnection.open(self.options, self.binding.thread) as connection:
            for part in self.incoming_frame(message).parts:
                await connection.deliver(part)
        return DeliveredOutcome

    async def show_sent(self, result: SendResult, target: str, body: str) -> None:
        async with OpenCodeConnection.open(self.options, self.binding.thread) as connection:
            await connection.toast(Toast(message=self.sent_text(result, target, body), variant='success'))

    async def report_failure(self, text: str) -> None:
        async with OpenCodeConnection.open(self.options, self.binding.thread) as connection:
            await connection.toast(Toast(message=text, variant='error'))


def main() -> None:
    parser = shared_parser(__doc__, Path.home() / '.local/state/opencode-comms-inbox', OpenCodeBridge)
    run_bridge(OpenCodeBridge, parse_options(parser, OpenCodeOptions))

