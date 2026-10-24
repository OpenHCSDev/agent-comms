"""Real Unix WebSocket replies, watch frames and Core pager; no replaced transports."""
import asyncio
import json
from contextlib import suppress

import pytest
from aiohttp import web

from agent_comms.cli_bridges.codex import CodexBridge, CodexOptions
from agent_comms.cli_bridges.shared import Binding, Watch
from agent_comms.runtime_updates import ThreadOutput
from agent_comms.cli_protocol import EarlierItems, WATCH_LINE
from agent_comms.comms import wire
from conftest import installed_comms
from agent_comms.coordination_errors import CoordinationReadUnavailable
from agent_comms.external_transcript import ExternalItemRow
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.threads import Thread


def entry(name, timestamp):
    return {'turnId': 'turn', 'completedAtMs': timestamp,
            'item': {'type': 'agentMessage', 'id': name, 'text': name}}


async def test_pager_requests_two_older_pages_through_the_bridges_real_socket(tmp_path):
    seen = []
    pages = {
        None: {'data': [entry('recent2', 5000), entry('recent1', 4000)], 'nextCursor': 'p1'},
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
    comms.registry.declare(Thread('cli', frozenset(), str(tmp_path),
                                                   execution=ExternalThreadExecution))
    transcript = comms.transcripts.external('cli')
    frames = asyncio.Queue()
    watching = asyncio.create_task(transcript.watch_requests(
        lambda row: frames.put_nowait(EarlierItems.from_row(row))))
    try:
        binding = Binding(str(comms.root), 'cli', 'thread', Binding.bus_head(str(comms.root)))
        with Binding.open(tmp_path / 'bridge') as db:
            options = CodexOptions(
                Watch(), binding, tmp_path / 'bridge', tmp_path, str(socket_path))
            bridge = CodexBridge(options, binding, db)
            output = ThreadOutput(transcript, comms.agents)
            output.publish_batch([update async for update in bridge.history()])
            assert seen == [None]
            newest = comms.transcripts.thread_transcript_page('cli', max_messages=1)
            assert [event.text for event in newest.events] == ['recent2']
            middle = comms.transcripts.thread_transcript_page('cli', before=newest.before, max_messages=1)
            assert [event.text for event in middle.events] == ['recent1']
            assert middle.has_older
            with pytest.raises(CoordinationReadUnavailable):
                comms.transcripts.thread_transcript_page('cli', before=middle.before)
            request = await asyncio.wait_for(frames.get(), 3)
            # The exact JSON line parser used by the running bridge owns decoding.
            request = WATCH_LINE.validate_json(request.model_dump_json())
            await request.handle(bridge, None)
            assert seen == [None, 'p1']
            # An already handled frame neither fetches nor republishes the page.
            await request.handle(bridge, None)
            assert seen == [None, 'p1']
            older2 = comms.transcripts.thread_transcript_page('cli', before=middle.before, max_messages=1)
            assert [event.text for event in older2.events] == ['older2']
            # Zero is a real external item coordinate, including a pinned read.
            older1 = comms.transcripts.thread_transcript_page('cli', before=older2.before,
                                                            through=middle.before, max_messages=1)
            assert [event.text for event in older1.events] == ['older1']
            with pytest.raises(CoordinationReadUnavailable):
                comms.transcripts.thread_transcript_page('cli', before=older1.before)
            request = await asyncio.wait_for(frames.get(), 3)
            await WATCH_LINE.validate_json(request.model_dump_json()).handle(bridge, None)
            oldest = comms.transcripts.thread_transcript_page('cli', before=older1.before)
            assert [event.text for event in oldest.events] == ['oldest']
            assert not oldest.has_older
            assert seen == [None, 'p1', 'p2']
            # Existing source coordinates survived both prepends.
            assert comms.transcripts.thread_transcript_page('cli', before=newest.before,
                                                           max_messages=1).events == middle.events
            assert [event.text for event in comms.transcripts.thread_transcript_page('cli').events] == [
                'oldest', 'older1', 'older2', 'recent1', 'recent2']
            with transcript.snapshot() as connection:
                positions = ExternalItemRow.select(connection, thread=transcript.lookup, order_by=('seq',))
            assert [row.seq for row in positions] == [-2, -1, 0, 1, 2]
    finally:
        watching.cancel()
        with suppress(asyncio.CancelledError):
            await watching
        await runner.cleanup()


async def test_rearmed_same_cursor_is_a_new_watch_request(tmp_path):
    from agent_comms.cli_bridges.codex_protocol import ItemsPage
    comms = wire(tmp_path / 'root')
    comms.registry.declare(Thread('cli', frozenset(), str(tmp_path)))
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
                 'clientId': BusInput.for_message('thread', 'm1', 'input').clientUserMessageId,
                 'content': [{'type': 'text', 'text': 'Already on the bus'}]},
    }], 'nextCursor': ''}).publication()
    item, = page.items
    assert item.update.page_record(item.turn_id, item.item_id).events == ()
    assert item.update.events[0].text == 'Already on the bus'
    assert page.earlier == EarlierPage('')  # An opaque empty cursor is still a string.


