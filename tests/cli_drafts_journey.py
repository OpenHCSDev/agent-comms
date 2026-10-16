"""Run real bridge transports/publication on a fresh root, with controlled CLI replies."""
from __future__ import annotations

import asyncio
from contextlib import aclosing, suppress
import json
import os
from pathlib import Path
import sys

from aiohttp import web


async def journey(root: Path) -> None:
    os.umask(0o077)
    for name in tuple(os.environ):
        if name.startswith('AGENT_COMMS_') or name.startswith('PI_SESSION_') or name.startswith('OPENCODE_SERVER_'):
            os.environ.pop(name)
    from agent_comms.comms import wire
    from agent_comms.cli_bridges.codex import CodexBridge, CodexOptions
    from agent_comms.cli_bridges.opencode import OpenCodeBridge, OpenCodeOptions
    from agent_comms.cli_bridges.shared import Binding, DeliveryJournal, Watch
    from agent_comms.thread_execution import ExternalThreadExecution
    from agent_comms.threads import Thread
    from agent_comms.wire_watch import WatchSource, WireWatch

    root.mkdir(mode=0o700)
    comms = wire(root)
    results = {}

    async def observed(source, predicate):
        sources = [WatchSource.modified_file(path) for path in source.source_paths(root)]
        async with asyncio.timeout(12):
            async with aclosing(WireWatch.observations(*sources)) as changes:
                async for _ in changes:
                    def read():
                        with source.reading() as snapshot:
                            return snapshot.current_draft(), tuple(snapshot.forward(0, snapshot.cut().offset))
                    value = await asyncio.to_thread(read)
                    if predicate(*value):
                        return value

    async def exercise(name, bridge, gates, runner):
        source = comms.transcripts.external(name)
        task = asyncio.create_task(bridge.publish_transcript())
        try:
            draft, _ = await observed(source, lambda draft, rows:
                                     draft is not None and draft.item_id == 'a'
                                     and draft.events[0].text == 'Hello world')
            assert draft.events[0].timestamp == 3.0
            gates[0].set()
            draft, rows = await observed(source, lambda draft, rows:
                                        any(event.text == 'barrier' for row in rows for event in row.events))
            assert draft is None, 'A late start/delta reopened a finished item'
            gates[1].set()
            draft, _ = await observed(source, lambda draft, rows:
                                     draft is not None and draft.item_id == 'r'
                                     and draft.events[0].text == 'One\n\nTwo plus')
            assert draft.events[0].timestamp == 5.0
            gates[2].set()
            draft, rows = await observed(source, lambda draft, rows:
                                        draft is None and any(event.text == 'One\n\nTwo plus!'
                                                              for row in rows for event in row.events))
            texts = [event.text for row in rows for event in row.events]
            assert texts == ['saved', 'Hello world!', 'barrier', 'One\n\nTwo plus!'], texts
            # Reopen through the normal canonical reader, not a test-maintained copy.
            reopened = wire(root).transcripts.external(name)
            with reopened.reading() as snapshot:
                assert snapshot.current_draft() is None
                assert [event.text for row in snapshot.forward(0, snapshot.cut().offset)
                        for event in row.events] == texts
            results[name] = dict(texts=texts, prefix_times=[3.0, 5.0], late_reopen=False)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            gates[3].set()
            await runner.cleanup()
            bridge.journal.close()

    # Codex's actual Unix WebSocket/JSON-RPC client and bridge.
    name = 'codex-drafts'
    comms.registry.declare(Thread(name, frozenset(), str(root), execution=ExternalThreadExecution))
    gates = [asyncio.Event() for _ in range(4)]
    async def websocket(request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        async def event(method, **params):
            await socket.send_json(dict(method=method, params=dict(threadId='source', **params)))
        async for frame in socket:
            packet = json.loads(frame.data)
            if 'id' not in packet:
                continue
            method = packet['method']
            if method == 'initialize':
                result = {}
            elif method == 'thread/resume':
                result = {'thread': {'id': 'source', 'status': {'type': 'active', 'activeFlags': []}}}
            elif method == 'thread/items/list':
                result = {'data': [{'turnId': 'old', 'startedAtMs': 1000, 'completedAtMs': 2000,
                                   'item': {'type': 'agentMessage', 'id': 'old', 'text': 'saved'}}]}
            else:
                raise AssertionError(f'Unexpected effectful Codex request {method}')
            await socket.send_json({'id': packet['id'], 'result': result})
            if method != 'thread/items/list':
                continue
            await event('item/started', turnId='t', startedAtMs=3000,
                        item={'type': 'agentMessage', 'id': 'a', 'text': ''})
            await event('item/agentMessage/delta', turnId='t', itemId='a', delta='Hello')
            await event('item/agentMessage/delta', turnId='t', itemId='a', delta=' world')
            await gates[0].wait()
            await event('item/completed', turnId='t', completedAtMs=4000,
                        item={'type': 'agentMessage', 'id': 'a', 'text': 'Hello world!'})
            await event('item/started', turnId='t', startedAtMs=3000,
                        item={'type': 'agentMessage', 'id': 'a', 'text': 'stale'})
            await event('item/agentMessage/delta', turnId='t', itemId='a', delta='late')
            await event('item/completed', turnId='t', completedAtMs=4001,
                        item={'type': 'plan', 'id': 'barrier', 'text': 'barrier'})
            await gates[1].wait()
            await event('item/started', turnId='t', startedAtMs=5000,
                        item={'type': 'reasoning', 'id': 'r', 'summary': []})
            for index, text in ((1, 'Two'), (0, 'One'), (1, ' plus')):
                await event('item/reasoning/summaryTextDelta', turnId='t', itemId='r',
                            summaryIndex=index, delta=text)
            await gates[2].wait()
            await event('item/completed', turnId='t', completedAtMs=6000,
                        item={'type': 'reasoning', 'id': 'r', 'summary': ['One', 'Two plus!']})
            await event('turn/completed', turn={'id': 't'})
            await gates[3].wait()
            break
        return socket

    application = web.Application()
    application.router.add_get('/', websocket)
    runner = web.AppRunner(application)
    await runner.setup()
    socket_path = root / 'codex.sock'
    await web.UnixSite(runner, str(socket_path)).start()
    state = root / 'codex-state'
    journal = DeliveryJournal(state)
    options = CodexOptions(Watch(), str(root), name, 'source', state, str(socket_path))
    await exercise(name, CodexBridge(options, Binding(str(root), name, 'source'), journal), gates, runner)

    # OpenCode's actual HTTP/SSE client and bridge, with saved history then live parts.
    name = 'opencode-drafts'
    comms.registry.declare(Thread(name, frozenset(), str(root), execution=ExternalThreadExecution))
    gates = [asyncio.Event() for _ in range(4)]
    ready = asyncio.Event()
    def info(identity, completed=None):
        return dict(id=identity, sessionID='source', role='assistant', parentID='prompt',
                    time=dict(created=3000 if identity == 'm' else 1000, completed=completed))
    def part(identity, text, start, end=None, kind='text', message='m'):
        return dict(type=kind, id=identity, messageID=message, sessionID='source', text=text,
                    time=dict(start=start, end=end))
    async def session(request):
        return web.json_response(dict(id='source', directory=str(root)))
    async def history(request):
        return web.json_response([dict(info=info('old', 2000),
                                       parts=[part('old', 'saved', 1000, 2000, message='old')])])
    async def message(request):
        return web.json_response(dict(info=info(request.match_info['message']), parts=[]))
    async def status(request):
        ready.set()
        return web.json_response({})
    async def events(request):
        response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
        await response.prepare(request)
        async def event(kind, properties=None):
            value = {'type': kind}
            if properties is not None:
                value['properties'] = properties
            await response.write(('data: ' + json.dumps(value) + '\n\n').encode())
        async def update(value):
            await event('message.part.updated', dict(sessionID='source', part=value))
        async def delta(identity, text):
            await event('message.part.delta', dict(sessionID='source', messageID='m',
                                                  partID=identity, field='text', delta=text))
        await event('server.connected')
        await ready.wait()
        await update(part('a', 'Hello', 3000))
        await delta('a', ' world')
        await gates[0].wait()
        await update(part('a', 'Hello world!', 3000, 4000))
        await update(part('a', 'stale', 3000))
        await delta('a', 'late')
        await update(part('barrier', 'barrier', 4001, 4002))
        await gates[1].wait()
        await update(part('r', 'One\n\nTwo', 5000, kind='reasoning'))
        await delta('r', ' plus')
        await gates[2].wait()
        await update(part('r', 'One\n\nTwo plus!', 5000, 6000, kind='reasoning'))
        await event('session.status', dict(sessionID='source', status={'type': 'idle'}))
        await gates[3].wait()
        return response

    application = web.Application()
    application.router.add_get('/event', events)
    application.router.add_get('/session/status', status)
    application.router.add_get('/session/source', session)
    application.router.add_get('/session/source/message', history)
    application.router.add_get('/session/source/message/{message}', message)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    state = root / 'opencode-state'
    journal = DeliveryJournal(state)
    options = OpenCodeOptions(Watch(), str(root), name, 'source', state, f'http://127.0.0.1:{port}', root)
    await exercise(name, OpenCodeBridge(options, Binding(str(root), name, 'source'), journal), gates, runner)
    print(json.dumps(dict(root=str(root), results=results, provider_inputs=0), indent=2))


if __name__ == '__main__':
    asyncio.run(journey(Path(sys.argv[1])))
