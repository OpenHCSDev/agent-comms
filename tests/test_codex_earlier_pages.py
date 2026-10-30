"""Real Unix WebSocket replies, watch frames and Core pager; no replaced transports."""
from agent_comms.repo import Checkout
import asyncio
import json
from contextlib import suppress

import pytest
from aiohttp import web

from agent_comms.cli_bridges.codex import CodexBridge, CodexOptions
from agent_comms.cli_bridges.codex_protocol import BusClientId
from agent_comms.field_codec import MessageId
from agent_comms.cli_bridges.shared import Binding, StoredBinding, Watch
from agent_comms.cli_protocol import WATCH_LINE, EarlierItems
from agent_comms.comms import wire
from agent_comms.coordination_errors import CoordinationReadUnavailable
from agent_comms.external_transcript import ExternalItemRow
from agent_comms.runtime_updates import ThreadOutput
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.threads import Thread
from conftest import installed_comms


def entry(name, timestamp):
    return {'turnId': 'turn', 'completedAtMs': timestamp,
            'item': {'type': 'agentMessage', 'id': name, 'text': name}}


async def test_pager_requests_two_older_pages_through_the_bridges_real_socket(tmp_path):
    seen = []
    historical_id = MessageId.mint('old-delivery-with-no-retained-attempt')
    historical_input = dict(turnId='old-turn', completedAtMs=4500, item=dict(
        type='userMessage', id='old-bus-input', clientId=BusClientId(historical_id).external,
        content=[dict(type='text', text='An old rendering, not the current IncomingFrame')]))
    pages = {
        None: {'data': [entry('recent2', 5000), historical_input, entry('recent1', 4000)], 'nextCursor': 'p1'},
        'p1': {'data': [entry('older2', 3000), entry('older1', 2000)], 'nextCursor': 'p2'},
        'p2': {'data': [entry('oldest', 1000)], 'nextCursor': None},
    }

    async def serve(request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        async for message in socket:
            frame = json.loads(message.data)
            method = frame['method']
            if method == 'initialized':
                continue
            if method == 'initialize':
                result = {'userAgent': 'codex/0.0.0'}
            elif method == 'thread/resume':
                result = {'thread': {'id': 'thread', 'status': {'type': 'idle'}}}
            else:
                assert method == 'thread/items/list'
                cursor = frame['params']['cursor']
                seen.append(cursor)
                result = pages[cursor]
            await socket.send_json({'id': frame['id'], 'result': result})
        return socket

    app = web.Application()
    app.router.add_get('/', serve)
    runner = web.AppRunner(app)
    await runner.setup()
    socket_path = tmp_path.parent / 'codex.sock'
    await web.UnixSite(runner, str(socket_path)).start()
    comms = installed_comms(tmp_path / 'root')
    comms.registry.declare(Thread('cli', frozenset(), Checkout(tmp_path),
                                                   execution=ExternalThreadExecution))
    transcript = comms.transcripts.external('cli')
    frames = asyncio.Queue()
    watching = asyncio.create_task(transcript.watch_requests(
        lambda row: frames.put_nowait(EarlierItems.from_row(row))))
    try:
        binding = Binding(root=str(comms.root), owner=comms.registry.require('cli').incarnation,
                          thread='thread', start=Binding.bus_head(str(comms.root)))
        with Binding.open(tmp_path / 'bridge') as db:
            options = CodexOptions(
                Watch(), StoredBinding(), tmp_path / 'bridge', tmp_path, str(socket_path))
            bridge = CodexBridge(options, binding.address(), db)
            await options.operation.prepare(bridge)
            output = ThreadOutput(transcript, comms.agents)
            output.publish_batch([await update.observed(bridge) async for update in bridge.history()])
            assert await bridge.attempts(historical_id) == ()
            assert seen == [None]
            newest = comms.transcripts.capture_page_read('cli').read()
            assert [event.text for event in newest.events] == ['recent1', 'recent2']
            assert newest.has_older
            with pytest.raises(CoordinationReadUnavailable):
                comms.transcripts.capture_page_read('cli', before=newest.before).read()
            request = await asyncio.wait_for(frames.get(), 3)
            # The exact JSON line parser used by the running bridge owns decoding.
            request = WATCH_LINE.validate_json(request.model_dump_json())
            await request.handle(bridge, None)
            assert seen == [None, 'p1']
            # An already handled frame neither fetches nor republishes the page.
            await request.handle(bridge, None)
            assert seen == [None, 'p1']
            # Zero is a real external item coordinate, including a pinned read.
            older = comms.transcripts.capture_page_read('cli', before=newest.before,
                                                        through=newest.before).read()
            assert [event.text for event in older.events] == ['older1', 'older2']
            with pytest.raises(CoordinationReadUnavailable):
                comms.transcripts.capture_page_read('cli', before=older.before).read()
            request = await asyncio.wait_for(frames.get(), 3)
            await WATCH_LINE.validate_json(request.model_dump_json()).handle(bridge, None)
            oldest = comms.transcripts.capture_page_read('cli', before=older.before).read()
            assert [event.text for event in oldest.events] == ['oldest']
            assert not oldest.has_older
            assert seen == [None, 'p1', 'p2']
            # Existing source coordinates survived both prepends.
            assert [event.text for event in comms.transcripts.capture_page_read(
                'cli', before=newest.before).read().events] == ['oldest', 'older1', 'older2']
            assert [event.text for event in comms.transcripts.capture_page_read('cli').read().events] == [
                'oldest', 'older1', 'older2', 'recent1', 'recent2']
            with transcript.snapshot() as connection:
                positions = ExternalItemRow.select(connection, thread=transcript.lookup, order_by=('seq',))
            assert [row.seq for row in positions] == [-2, -1, 0, 1, 2, 3]
            # Resume passes historical inputs through PublishedPage, never live confirmation.
            output.publish_batch([await update.observed(bridge) async for update in bridge.history()])
            assert await bridge.attempts(historical_id) == ()
    finally:
        watching.cancel()
        with suppress(asyncio.CancelledError):
            await watching
        await runner.cleanup()


async def test_rearmed_same_cursor_is_a_new_watch_request(tmp_path):
    from agent_comms.cli_bridges.codex_protocol import ItemsPage
    comms = wire(tmp_path / 'root')
    comms.registry.declare(Thread('cli', frozenset(), Checkout(tmp_path)))
    transcript = comms.transcripts.external('cli')
    output = ThreadOutput(transcript, comms.agents)
    page = ItemsPage.model_validate({'data': [entry('new', 2000)], 'nextCursor': 'p1'}).publication()
    page.publish(output)
    frames = asyncio.Queue()
    watching = asyncio.create_task(transcript.watch_requests(
        lambda row: frames.put_nowait(EarlierItems.from_row(row))))
    try:
        with transcript.reading() as records:
            assert not records.request_earlier(1)  # The local item has not been consumed.
            assert records.request_earlier(0)
        first = await asyncio.wait_for(frames.get(), 3)
        # A reconnect republishes the newest snapshot while the old frame is queued.
        page.publish(output)
        with transcript.reading() as records:
            assert records.request_earlier(0)
        second = await asyncio.wait_for(frames.get(), 3)
        assert first.request.request_id != second.request.request_id
        with transcript.reading() as records:
            assert not records.pending(first) and records.pending(second)
    finally:
        watching.cancel()
        with suppress(asyncio.CancelledError):
            await watching


def test_hidden_page_items_keep_import_events_but_own_no_visible_events(tmp_path):
    from agent_comms.cli_bridges.codex_protocol import BusInput, ItemsPage
    from agent_comms.external_transcript import EarlierPage
    page = ItemsPage.model_validate({'data': [{
        'turnId': 't', 'completedAtMs': 1000,
        'item': {'type': 'userMessage', 'id': 'bus',
                 'clientId': BusInput.for_message('thread', MessageId.mint('hidden-page-input'), 'input').clientUserMessageId,
                 'content': [{'type': 'text', 'text': 'Already on the bus'}]},
    }], 'nextCursor': ''}).publication()
    item, = page.items
    assert item.update.page_record(item.turn_id, item.item_id).events == ()
    assert item.update.events[0].text == 'Already on the bus'
    assert page.earlier == EarlierPage('')  # An opaque empty cursor is still a string.




async def test_draining_server_gets_the_same_turn_after_reconnecting(tmp_path):
    """Codex refuses a turn while draining and asks for it again; the input is delivered, not lost."""
    from agent_comms.cli_protocol import DeliveredOutcome
    from agent_comms.messages import InfoKind, Message

    turns = []

    async def serve(request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        async for message in socket:
            frame = json.loads(message.data)
            if frame['method'] == 'initialized':
                continue
            if frame['method'] == 'initialize':
                await socket.send_json({'id': frame['id'], 'result': {'userAgent': 'codex/0.0.0'}})
                continue
            assert frame['method'] == 'turn/start'
            turns.append(frame['params'])
            reply = ({'error': {'code': -32000, 'message': 'Server is draining; retry after reconnecting'}}
                     if len(turns) == 1 else {'result': {}})
            await socket.send_json({'id': frame['id'], **reply})
        return socket

    app = web.Application()
    app.router.add_get('/', serve)
    runner = web.AppRunner(app)
    await runner.setup()
    socket_path = tmp_path.parent / 'draining-codex.sock'
    await web.UnixSite(runner, str(socket_path)).start()
    comms = installed_comms(tmp_path / 'root')
    comms.registry.declare(Thread('cli', frozenset(), Checkout(tmp_path), execution=ExternalThreadExecution))
    try:
        binding = Binding(root=str(comms.root), owner=comms.registry.require('cli').incarnation,
                          thread='thread', start=Binding.bus_head(str(comms.root)))
        with Binding.open(tmp_path / 'bridge') as db:
            options = CodexOptions(Watch(), StoredBinding(), tmp_path / 'bridge', tmp_path, str(socket_path))
            bridge = CodexBridge(options, binding.address(), db)
            message = Message(sender='sender', target='cli', body='keep going', type=InfoKind)
            assert await bridge.deliver(message) is DeliveredOutcome
        assert len(turns) == 2 and turns[0] == turns[1]
        assert turns[0]['clientUserMessageId'] == f'comms:{message.message_id}'
    finally:
        await runner.cleanup()
