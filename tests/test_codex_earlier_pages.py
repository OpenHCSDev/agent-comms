"""Real Unix WebSocket replies, watch frames and Core pager; no replaced transports."""
import asyncio
import json
from contextlib import suppress

import pytest
from aiohttp import web

from agent_comms.cli_bridges.codex import CodexBridge, CodexOptions
from agent_comms.cli_bridges.shared import Binding, ThreadOutput, Watch
from agent_comms.cli_protocol import EarlierItems, WATCH_LINE
from agent_comms.comms import wire
from agent_comms.coordination_errors import CoordinationReadUnavailable
from agent_comms.external_transcript import ExternalItemRow
from agent_comms.thread_execution import ExternalThreadExecution


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
                result = {}
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
    comms = wire(tmp_path / 'root')
    comms.registry.declare(comms.registry.new_thread('cli', frozenset(), str(tmp_path),
                                                   execution=ExternalThreadExecution))
    transcript = comms.transcripts.external('cli')
    frames = asyncio.Queue()
    watching = asyncio.create_task(transcript.watch_requests(
        lambda row: frames.put_nowait(EarlierItems.from_row(row))))
    try:
        binding = Binding(str(comms.root), 'cli', 'thread')
        with Binding.open(tmp_path / 'bridge') as db:
            bridge = CodexBridge(CodexOptions(Watch(), binding, tmp_path / 'bridge', str(socket_path)), binding, db)
            output = ThreadOutput(transcript, comms.agents, 'cli')
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
    comms.registry.declare(comms.registry.new_thread('cli', frozenset(), str(tmp_path)))
    transcript = comms.transcripts.external('cli')
    output = ThreadOutput(transcript, comms.agents, 'cli')
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


def test_stopped_carry_preserves_external_items(tmp_path):
    import json
    from contextlib import closing
    from agent_comms.coordination_database import CoordinationStore
    from agent_comms.external_transcript import ExternalTranscript, NoEarlierPage
    from agent_comms.field_codec import FieldCodec
    from agent_comms.transcript_events import AssistantTranscript
    from tools.cutover.carry_external_pages import ExternalPages

    root = tmp_path / 'root'
    root.mkdir()
    path = ExternalTranscript.path_in(root)
    saved = json.dumps(FieldCodec.encode((AssistantTranscript('preserved', position='i'),)))
    # This historical conversion predates the separate event table.
    with closing(CoordinationStore.writer(path)) as connection:
        connection.execute('CREATE TABLE external_item (thread TEXT NOT NULL, turn_id TEXT NOT NULL, '
                           'item_id TEXT NOT NULL, seq INTEGER NOT NULL, events TEXT NOT NULL, '
                           'PRIMARY KEY(thread,turn_id,item_id), UNIQUE(thread,seq)) STRICT')
        connection.execute('INSERT INTO external_item VALUES (?,?,?,?,?)', ('cli', 't', 'i', 1, saved))
        connection.commit()
    assert ExternalPages.carry(root, apply=False).changed == 1
    assert ExternalPages.carry(root, apply=True).changed == 1
    assert ExternalPages.carry(root, apply=True).changed == 0
    with closing(CoordinationStore.writer(path)) as connection:
        row, = connection.execute('SELECT thread,turn_id,item_id,seq,events,earlier FROM external_item')
    assert row[:5] == ('cli', 't', 'i', 1, saved)
    assert FieldCodec.decode(NoEarlierPage, json.loads(row[5])) == NoEarlierPage()
