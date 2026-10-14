#!/home/ts/.local/share/agent-comms/runtimes/toad-86d9e5c49-core-ccec96b85-native-dba4356e87520876-20261014T165633Z/bin/python
"""Connect a running OpenCode HTTP session to the shared Core CLI bridge."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import os
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import quote

import aiohttp
from agent_comms.messages import Message

from cli_comms import (
    CliBridge, Options, Outcome, SendResult, parse_options, run_bridge, shared_parser,
)
from opencode_comms_protocol import (
    Connected, OpenCodeEvent, Prompt, SavedInput, Session, TextInput, Toast, UserMessage,
)


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
    raise ConnectionError('OpenCode event stream ended before input confirmation')


@dataclass
class OpenCodeOptions(Options):
    server: str
    directory: Path


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


class OpenCodeBridge(CliBridge[OpenCodeOptions]):
    async def deliver(self, message: Message) -> Outcome:
        async with OpenCodeConnection.open(self.options, self.binding.thread) as connection:
            await connection.deliver(self.incoming_text(message))
        return Outcome.DELIVERED

    async def show_sent(self, result: SendResult, target: str, body: str) -> None:
        async with OpenCodeConnection.open(self.options, self.binding.thread) as connection:
            await connection.toast(Toast(message=self.sent_text(result, target, body), variant='success'))

    async def report_failure(self, text: str) -> None:
        async with OpenCodeConnection.open(self.options, self.binding.thread) as connection:
            await connection.toast(Toast(message=text, variant='error'))


def main() -> None:
    parser = shared_parser(__doc__, Path.home() / '.local/state/opencode-comms-inbox', OpenCodeBridge)
    parser.add_argument('--server', required=True, help='HTTP address of the running OpenCode TUI/server')
    parser.add_argument('--directory', type=Path, default=Path.cwd(), help='OpenCode session working directory')
    run_bridge(OpenCodeBridge, parse_options(parser, OpenCodeOptions))


if __name__ == '__main__':
    main()
