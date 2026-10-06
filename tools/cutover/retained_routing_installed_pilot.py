"""Genuine old720 routing → mounted recorded selection and notification reads."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
from unittest.mock import patch

from agent_comms.comms import Comms
from agent_comms.message_reference import MessageReference
from agent_comms.store_files import _store_lock
from agent_comms.field_codec import FieldCodec
from agent_comms.historical_views import HistorySource
from routing_carry import file_witness
from retained_routing_cutover import RetainedRoutingCutover


async def render(service, source, reference):
    from agent_comms.transcripts import RecordedTranscriptReadIdentity, Transcripts
    from toad.app import ToadApp
    from toad.core.source_events import MessageHandlingRequested
    from toad.screens.historical_sessions import HistoricalSessions
    from toad.widgets.history_anchor import HistoryWindow
    from toad.widgets.message_notifications import MessageNotifications
    from toad.widgets.transcript_history import TranscriptHistory
    from toad.widgets.wire_message_handling import WireMessageHandling
    from textual.widgets import Select
    from textual.worker import WorkerCancelled, WorkerState

    threads = service.views.historical_threads()
    alpha, beta = (next(index for index, item in enumerate(threads)
                        if item.source.key == source.key and item.thread.name == name)
                   for name in ('alpha', 'beta'))
    captures, selections, retirements = [], [], []
    capture = Transcripts.capture_page_read

    def captured(owner, name, **kwargs):
        read = capture(owner, name, **kwargs)
        if kwargs.get('historical_source') == source.key:
            assert isinstance(read.identity, RecordedTranscriptReadIdentity)
            assert read.identity.thread.incarnation == source.provenance.require(name).incarnation
            captures.append({'name': name, 'identity': FieldCodec.encode(read.identity)})
        return read

    app = ToadApp(project_dir=str(service.root.parent), mode='store')
    screen = HistoricalSessions(service, threads, name='alpha', source=source.key)
    with patch.object(Transcripts, 'capture_page_read', captured):
        async with app.run_test(size=(120, 40)) as pilot:
            await app.push_screen(screen)
            await pilot.pause()
            selector = screen.query_one('#saved-identity', Select)

            async def inspect(index):
                await pilot.pause()
                workers = tuple(worker for worker in app.workers
                                if worker.node is screen and worker.group == 'historical-handling')
                for worker in workers:
                    await worker.wait()
                await pilot.pause()
                assert selector.value == index and screen.is_attached
                item = threads[index]
                history = screen.query_one(TranscriptHistory)
                assert any(row['name'] == item.thread.name for row in captures)
                assert any(reference in event.incoming_sources
                           for page in history.pages for event in page.page.events)
                bodies = WireMessageHandling.within(screen.query_one('#saved-content'))
                assert bodies and reference in WireMessageHandling.references_in(bodies)
                expected = item.source.notification_references(
                    service.bus.history, WireMessageHandling.references_in(bodies))
                for body in bodies:
                    feedback = body.query_one(MessageNotifications)
                    assert feedback._error is None
                    assert feedback._notifications == tuple(notification
                        for ref in body.handling_references
                        for notification in expected[ref.seq, ref.message_id])
                    assert feedback._notifications
                    assert all(not notification.busy for notification in feedback._notifications)
                selections.append({'name': item.thread.name, 'source': item.source.key,
                                   'incarnation': FieldCodec.encode(item.thread.incarnation),
                                   'notifications': [
                                       FieldCodec.encode(body.query_one(MessageNotifications)._notifications)
                                       for body in bodies]})
                return bodies

            await inspect(alpha)
            window = screen.query_one(HistoryWindow)
            window.scroll_end(animate=False, immediate=True)
            await pilot.wait_for_scheduled_animations()
            await pilot.pause()
            region = window.scrollable_content_region
            painted = '\n'.join(strip.crop(region.x, region.right).text for strip in
                screen._compositor.render_strips()[region.y:region.bottom])
            assert painted.count('Retained native answer') == 1, painted
            assert painted.count('Retained original frozen audience') == 1, painted

            async def retire(kind):
                # Delegate the actual source owner, holding its completed read
                # at return. Selection/unmount still owns the native worker.
                bodies = WireMessageHandling.within(screen.query_one('#saved-content'))
                entered, finished = asyncio.Event(), asyncio.Event()
                released = threading.Event()
                loop = asyncio.get_running_loop()
                original = HistorySource.notification_references
                shown, errors = [], []
                show, show_error = (WireMessageHandling.show_notifications,
                                    WireMessageHandling.show_notification_error)
                held = False

                def acquire(captured_source, archive, references):
                    nonlocal held
                    if held or captured_source != source:
                        return original(captured_source, archive, references)
                    held = True
                    try:
                        result = original(captured_source, archive, references)
                        loop.call_soon_threadsafe(entered.set)
                        released.wait()
                        return result
                    finally:
                        loop.call_soon_threadsafe(finished.set)

                def published(body, results):
                    if body in bodies and entered.is_set():
                        shown.append(body)
                    return show(body, results)

                def failed(body, error):
                    if body in bodies and entered.is_set():
                        errors.append(body)
                    return show_error(body, error)

                with patch.object(HistorySource, 'notification_references', acquire), \
                     patch.object(WireMessageHandling, 'show_notifications', published), \
                     patch.object(WireMessageHandling, 'show_notification_error', failed):
                    try:
                        bodies[0].publish_core(MessageHandlingRequested())
                        await entered.wait()
                        worker, = tuple(worker for worker in app.workers
                                        if worker.node is screen and worker.group == 'historical-handling')
                        generation = screen._selection_generation
                        if kind == 'selection':
                            selector.value = beta
                        else:
                            await pilot.press('escape')
                        await pilot.pause()
                        try:
                            await worker.wait()
                        except WorkerCancelled:
                            pass
                        else:
                            raise AssertionError('Retired historical worker completed successfully')
                        assert worker.state is WorkerState.CANCELLED
                        if kind == 'selection':
                            assert screen._selection_generation > generation and selector.value == beta
                        else:
                            assert not screen.is_attached
                        assert all(not body.is_attached for body in bodies)
                    finally:
                        released.set()
                        await finished.wait()
                    await pilot.pause()
                    assert not shown and not errors
                    retirements.append({'kind': kind, 'worker': worker.state.name,
                                        'completed_original_read_returned': True,
                                        'stale_notification_publications': 0})

            await retire('selection')
            await inspect(beta)
            selector.value = alpha
            await inspect(alpha)
            await retire('screen')
            assert app._exception is None
        assert app.preparation._closed
        assert not app.workers
    await asyncio.get_running_loop().shutdown_default_executor()
    assert {row['name'] for row in captures} >= {'alpha', 'beta'}
    return {'native_answer_painted_once': True, 'original_request_painted_once': True,
            'mounted_historical_selections': selections, 'recorded_page_captures': captures,
            'notification_retirements': retirements, 'app_preparation_and_workers_closed': True,
            'original_read_default_executor_joined': True}


def main():
    stage = Path(os.environ['AC_ROUTING_FIXTURE_STAGE']).absolute()
    stage.mkdir(parents=True, exist_ok=False)
    root = stage / 'wire'
    old_python = Path(os.environ['AC_INDEX_ORIGINAL_PYTHON'])
    package = Path(os.environ['AC_NATIVE_PACKAGE'])
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    seeded = subprocess.run([str(old_python),
        str(Path(__file__).with_name('seed_retained_routing_fixture.py')), str(root), str(package)],
        env=environment, capture_output=True, text=True)
    if seeded.returncode:
        sys.stderr.write(seeded.stderr)
    seeded.check_returncode()
    seed = json.loads(seeded.stdout)
    os.environ.update(AGENT_COMMS_ROOT=str(root), XDG_CONFIG_HOME=str(stage / 'config'),
                      XDG_STATE_HOME=str(stage / 'state'), XDG_DATA_HOME=str(stage / 'data'))
    database = root / 'transcript_routes.sqlite3'
    inputs_before = file_witness(root / 'input_dispositions.json')
    native_before = file_witness(Path(seed['session']))
    original_bus = file_witness(root / 'bus.jsonl')
    cutover = RetainedRoutingCutover(old_python, seed['root_id'], package, stage / 'carry-receipt.json')
    cutover.require_source()
    if '--reject-corrupt-original' in sys.argv:
        # Corrupt the last genuine old annotation, after two earlier valid
        # cells. The operation must refuse the whole plan before any writes.
        with sqlite3.connect(database) as db:
            old = json.loads(db.execute('SELECT routing FROM transcript_route WHERE entry_id=?',
                                        (seed['final'],)).fetchone()[0])
            old['requests'][0]['id'] = 'f' * 32
            db.execute('UPDATE transcript_route SET routing=? WHERE entry_id=?',
                       (json.dumps(old), seed['final']))
        original_files = {str(path): file_witness(path) for path in root.rglob('*') if path.is_file()}
        try:
            with _store_lock(root / 'wire'):
                cutover.quiet_install(root)
        except subprocess.CalledProcessError as error:
            assert error.returncode != 0
        else:
            raise AssertionError('Corrupt original request was admitted')
        assert all(file_witness(Path(path)) == witness for path, witness in original_files.items())
        assert not (stage / 'carry-receipt.json').exists()
        assert not (stage / 'carry-receipt.json.originals').exists()
        receipt = {'corrupt_original_refused_before_mutation': True,
                   'earlier_valid_cells_not_partially_carried': True,
                   'all_original_files_unchanged': True, 'provider_calls': 0, 'input_replays': 0}
        (stage / 'receipt.json').write_text(json.dumps(receipt, indent=2))
        print(json.dumps(receipt), flush=True)
        return
    with _store_lock(root / 'wire'):
        cutover.quiet_install(root)
    assert file_witness(root / 'input_dispositions.json') == inputs_before
    assert file_witness(Path(seed['session'])) == native_before
    assert file_witness(root / 'bus.jsonl') == original_bus
    with sqlite3.connect(database) as db:
        bindings = list(db.execute('SELECT native_id,text,sent_text_digest,routing FROM input_display'))
        assert len(bindings) == 2
        display = next(row for row in bindings if row[0] == 'a' * 32)
        assert display[1] == 'Retained displayed input' and display[2]
        assert json.loads(display[3])['requests'] == [
            {'seq': seed['original']['seq'], 'message_id': seed['original']['id']}]
        assert 'publications' not in json.loads(display[3])
        assert all(json.loads(row[0])['requests'] == json.loads(display[3])['requests']
                   for row in db.execute('SELECT routing FROM transcript_route'))
    service = Comms(stage / 'reader')
    receipt = json.loads((stage / 'carry-receipt.json').read_text())
    acquired = FieldCodec.decode(HistorySource, receipt['recorded_source'])
    source = service.views.attach_history(root, source_read=acquired)
    read = service.transcripts.capture_page_read('alpha', historical_source=source.key)
    page = service.transcripts.bind_page_read('alpha', read.identity).read()
    reference = MessageReference(seed['original']['seq'], seed['original']['id'])
    assert any(reference in event.incoming_sources for event in page.events)
    assert not any('c' * 32 in event.native_inputs for event in page.events)
    os.environ['AGENT_COMMS_ROOT'] = str(service.root)
    protected = {str(path): file_witness(path) for directory in (root, Path(source.root))
                 for path in directory.rglob('*') if path.is_file()}
    result = asyncio.run(render(service, source, reference))
    assert all(file_witness(Path(path)) == witness for path, witness in protected.items())
    assert receipt['routing_cells'] == 3 and receipt['registry_routings'] == 1
    (stage / 'receipt.json').write_text(json.dumps({**result,
        'original_input_unknown_unchanged': True, 'source_bytes_unchanged': True,
        'central_batch_used': False, 'joined_authored_seed': True, 'new_owner_launches': 0,
        'provider_calls': 0, 'input_replays': 0,
        'routing_cells': receipt['routing_cells'], 'registry_routings': receipt['registry_routings']}, indent=2))
    print(json.dumps(json.loads((stage / 'receipt.json').read_text())), flush=True)


if __name__ == '__main__':
    main()
