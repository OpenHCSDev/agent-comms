"""Replay retained OpenCode parts through installed HTTP and event decoders.

Original files are only read. The local server serves their saved message
metadata; it accepts GET only and cannot submit an input to OpenCode.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

import aiohttp
from aiohttp import web
import agent_comms
from agent_comms.cli_bridges.opencode import OpenCodeConnection
from agent_comms.cli_bridges.opencode_parts import PendingTool, ToolPart
from agent_comms.cli_bridges.opencode_protocol import OpenCodeEvent, OpenCodeStream
from agent_comms.cli_bridges.shared import PendingToolCall
from agent_comms.field_codec import FieldCodec
from agent_comms.transcript_events import ToolPendingTranscript


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_pending(updates):
    update, = updates
    assert isinstance(update.accumulator, PendingToolCall)
    assert not hasattr(update.accumulator, 'arguments')
    visible, = update.accumulator.events
    assert visible.title == update.detail == update.accumulator.title
    assert isinstance(visible, ToolPendingTranscript)
    assert not {'raw_input', 'detail', 'path'} & FieldCodec.encode(visible).keys()


async def run(options):
    core = Path(agent_comms.__file__).resolve()
    assert core.is_relative_to(options.installed.resolve()), core
    options.output.mkdir(mode=0o700)
    messages = {}
    originals = {}
    for source in sorted(options.messages.glob('*.json')):
        originals[source] = digest(source)
        info = json.loads(source.read_bytes())
        parts = []
        for part in sorted((options.parts / info['id']).glob('*.json')):
            originals[part] = digest(part)
            parts.append(json.loads(part.read_bytes()))
        messages[info['id']] = dict(info=info, parts=parts)
    assert messages, options.messages
    session = options.messages.name
    originals[options.pending_part] = digest(options.pending_part)
    requests = []

    async def saved_message(request):
        requests.append(request.method)
        assert request.match_info['session'] == session
        return web.json_response(messages[request.match_info['message']])

    app = web.Application()
    app.router.add_get('/session/{session}/message/{message}', saved_message)
    runner = web.AppRunner(app)
    report = dict(state='unfinished', core=str(core), messages=len(messages), parts=0,
                  pending_parts=0, updates=0, errors=0, provider_inputs=0)
    try:
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        host, port = runner.addresses[0]
        async with aiohttp.ClientSession(base_url=f'http://{host}:{port}') as http:
            connection = OpenCodeConnection(http, session)
            stream = OpenCodeStream(connection)
            for message in messages.values():
                saved = await connection.message(message['info']['id'])
                report['updates'] += len(stream.snapshot(saved))
                for raw in message['parts']:
                    event = OpenCodeEvent.decode(json.dumps(dict(type='message.part.updated',
                        properties=dict(sessionID=session, part=raw))))
                    updates = await event.receive(stream)
                    report['parts'] += 1
                    report['updates'] += len(updates)
                    part = event.properties.part
                    if isinstance(part, ToolPart) and isinstance(part.state, PendingTool):
                        report['pending_parts'] += 1
                        check_pending(updates)
        report['http_pending_parts'] = report['pending_parts']
        # This native pending Edit survives as a part file, but its original
        # parent header is gone. Do not fabricate one: tool states take their
        # turn identity from the original part itself, as the live path does.
        pending = ToolPart.model_validate_json(options.pending_part.read_bytes())
        assert pending.sessionID == session and isinstance(pending.state, PendingTool)
        check_pending(pending.state.present(pending))
        report.update(pending_parts=report['pending_parts'] + 1, orphan_pending_parts=1,
            pending_scope='actual orphan part decoder and renderer; no parent-header or full pending-stream claim')
        assert report['pending_parts'] > 0
        assert all(method == 'GET' for method in requests)
        assert all(digest(path) == original for path, original in originals.items())
        report.update(state='PASS', http_gets=len(requests), originals_unchanged=True,
                      original_files=len(originals), source=str(options.messages))
    except BaseException as error:
        report.update(state='FAILED', errors=1, error=repr(error))
        raise
    finally:
        await runner.cleanup()
        report['server_retired'] = True
        (options.output / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
        (options.output / 'source-hashes.json').write_text(json.dumps(
            {str(path): value for path, value in originals.items()}, indent=2) + '\n')
        print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--messages', required=True, type=Path)
    parser.add_argument('--parts', required=True, type=Path)
    parser.add_argument('--pending-part', required=True, type=Path)
    parser.add_argument('--installed', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    asyncio.run(run(parser.parse_args()))
