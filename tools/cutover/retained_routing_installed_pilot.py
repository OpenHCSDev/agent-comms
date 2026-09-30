"""Provider-free authentic old720 → installed1f reader/renderer acceptance."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from agent_comms.comms import Comms
from agent_comms.message_reference import MessageReference
from routing_carry import file_witness
from retained_routing_cutover import RetainedRoutingCutover


async def render(page, root):
    from toad.app import ToadApp
    from toad.widgets.transcript_history import TranscriptHistory

    app = ToadApp(project_dir=str(root.parent))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        conversation = app.selected_session.conversation
        await conversation.contents.remove_children()
        history = TranscriptHistory(page)
        await conversation.post(history)
        conversation.window.scroll_end(animate=False, immediate=True)
        await pilot.pause(0.8)
        region = conversation.window.scrollable_content_region
        painted = '\n'.join(strip.crop(region.x, region.right).text for strip in
            app.screen._compositor.render_strips()[region.y:region.bottom])
        assert painted.count('Retained native answer') == 1, painted
        assert painted.count('Retained original frozen audience') == 1, painted
        return {'native_answer_painted_once': True, 'original_request_painted_once': True}


def main():
    stage = Path(os.environ['AC_ROUTING_FIXTURE_STAGE']).absolute()
    stage.mkdir(parents=True, exist_ok=False)
    root = stage / 'wire'
    old_python = Path(os.environ['AC_INDEX_ORIGINAL_PYTHON'])
    package = Path(os.environ['AC_NATIVE_COPIED_PACKAGE'])
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
    service = Comms(root)
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
            service.owners.restart_owners(cutover=cutover)
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
    assert service.owners.restart_owners(cutover=cutover) == ()
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
    page = service.transcripts.capture_page_read('alpha').read()
    reference = MessageReference(seed['original']['seq'], seed['original']['id'])
    assert any(reference in event.incoming_sources for event in page.events)
    assert not any('c' * 32 in event.native_inputs for event in page.events)
    result = asyncio.run(render(page, root))
    receipt = json.loads((stage / 'carry-receipt.json').read_text())
    assert receipt['routing_cells'] == 3 and receipt['registry_routings'] == 1
    (stage / 'receipt.json').write_text(json.dumps({**result,
        'original_input_unknown_unchanged': True, 'source_bytes_unchanged': True,
        'central_batch_used': True, 'provider_calls': 0, 'input_replays': 0,
        'routing_cells': receipt['routing_cells'], 'registry_routings': receipt['registry_routings']}, indent=2))
    print(json.dumps(json.loads((stage / 'receipt.json').read_text())), flush=True)


if __name__ == '__main__':
    main()
