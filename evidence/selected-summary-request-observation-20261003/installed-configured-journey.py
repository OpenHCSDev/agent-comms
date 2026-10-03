"""Reuse the existing configured saved-source SDK journey with native clocks."""
import asyncio
import hashlib
from importlib import metadata
import json
from pathlib import Path
import subprocess
import sys

here = Path(__file__).resolve().parent
checkout = here.parents[1]
sys.path.insert(0, str(checkout / 'tests'))
sys.path.insert(0, str(checkout / 'tools/cutover'))
from original_owner_capture import CurrentTypedCapture
from compaction_source_successor_installed_journey import run
import agent_comms
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.field_codec import FieldCodec
from agent_comms.input_disposition import InputDispositions
from agent_comms.pi_vocabulary import ThinkingLevel

stage = Path('/home/ts/.cache/agent-scratch/m555')
package = Path('/home/ts/wt/comms-task-aware-native-bundle-20261002/stack/.pi-native-de16647979cbad28/node_modules/@earendil-works/pi-coding-agent')
original_python = Path('/home/ts/wt/toad-s5-receiving-boundary-pair-20261001/.artifacts/runtime-historical-handling290-current-20261001/bin/python')

def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

async def main():
    installed = Path(agent_comms.__file__).parent
    assert installed.is_relative_to(Path(sys.prefix))
    source = checkout / 'src/agent_comms'
    hashes = {str(path.relative_to(source)): digest(path) for path in source.rglob('*')
              if path.is_file() and '__pycache__' not in path.parts}
    for name, expected in hashes.items():
        assert digest(installed / name) == expected, name
    (here / 'installed-source-proof.json').write_text(json.dumps(hashes, indent=2) + '\n')
    captured = CurrentTypedCapture(Path('/var/tmp/agent-comms-live-20260927-wzjtqhza'), original_python).read('openhcs-audit-merged-boundaries')
    original = captured.require_current()
    original.require_idle()
    source_file = Path(original.require_saved_session())
    protected = [source_file, Path(str(source_file) + '.input-proof')]
    diagnostic = Path('/var/tmp/agent-comms-live-20260927-wzjtqhza/diagnostics/79cb9232873a4501a118cc25da9fca67.json')
    if diagnostic.is_file():
        protected.append(diagnostic)
    before = {str(path): digest(path) for path in protected}
    intent = {'source_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=checkout, text=True).strip(),
              'python': sys.executable, 'package': str(package), 'original_python': str(original_python),
              'model': original.model, 'thinking': ThinkingLevel.optional_name(original.thinking_level),
              'original_selection': FieldCodec.encode(captured.selection),
              'settings_paths': list(captured.retained.configuration.settings_paths(Path(original.worktree))),
              'protected_before': before, 'source_bytes': source_file.stat().st_size,
              'direct_url': json.loads(metadata.distribution('agent-comms').read_text('direct_url.json')),
              'sdk': metadata.version('agent-client-protocol'), 'source_files_equal': len(hashes),
              'public_inputs': 0, 'replay': False,
              'scope': 'One configured isolated saved-session fork, SDK/ACP manual selected summary, private peer during streaming, then distinct new input; no physical UI or public channel performance claim.'}
    (here / 'installed-intent.json').write_text(json.dumps(intent, indent=2) + '\n')
    def original_capture():
        return captured.require_current(), captured.retained
    try:
        await run(stage, package, source_file, capture_source=original_capture,
                  probe_marker='SOURCE555_DISTINCT_AFTER_SELECTED_SUMMARY')
    finally:
        after = {str(path): digest(path) for path in protected}
        (here / 'installed-protected.json').write_text(json.dumps({
            'before': before, 'after': after, 'equal': before == after}, indent=2) + '\n')
    assert before == after
    journal = CompactionJournal(stage / 'wire/compaction-commits.sqlite3')
    original_receipt = json.loads((stage / 'receipt.json').read_text())
    assert original_receipt['complete'] and original_receipt['native_children_closed']
    diagnostic_files = sorted((stage / 'wire/diagnostics').glob('*.requests.jsonl'))
    requests = [json.loads(line) for path in diagnostic_files for line in path.read_text().splitlines()]
    selected = [row for row in requests if 'selected_summary' in row]
    normal = [row for row in requests if 'native' in row and 'selected_summary' not in row]
    assert selected and normal
    inputs = InputDispositions(stage / 'wire' / InputDispositions.filename).read()
    assert len(inputs.rows) == 1
    grouped = {}
    for row in selected:
        native = row['native']
        grouped.setdefault(native['requestId'], []).append(row)
    results = []
    for request_id, rows in grouped.items():
        first = rows[0]
        identity = first['selected_summary']
        attempt = journal.summaries.get(identity['operation_id'])
        assert identity['session_file'] == attempt.session_file
        assert all(row['selected_summary'] == identity and row['turn'] == first['turn']
                   and row['native_process'] == first['native_process'] for row in rows)
        stages = [{'stage': row['native']['stage'], 'elapsed_ms': row['native']['elapsedMs'],
                   'transport': row['native']['transport'], 'callback': row['native']['callback'],
                   'attempt': row['native']['attempt'], 'status': row['native'].get('status'),
                   'receipt_monotonic_ns': row['recorded_monotonic_ns']} for row in rows]
        labels = {item['stage'] for item in stages}
        assert {'preparing', 'finished', 'first_delta_consumed'} <= labels, labels
        results.append({'request_id': request_id, 'operation': identity,
                        'turn': first['turn'], 'native_process': first['native_process'], 'phases': stages})
    selected_leases = {json.dumps(row['turn'], sort_keys=True) for row in selected}
    assert all(json.dumps(row['turn'], sort_keys=True) not in selected_leases for row in normal)
    for path in diagnostic_files:
        (here / ('installed-' + path.name)).write_bytes(path.read_bytes())
    receipt = {'result': 'CONFIGURED_SELECTED_SUMMARY_CLOCKS_AND_DISTINCT_TURN_PASS',
               'intent': intent, 'journey': original_receipt, 'selected_requests': results,
               'normal_request_count': len({row['native']['requestId'] for row in normal}),
               'subsequent_turns_distinct_from_selected_summary': True,
               'original_protected_unchanged': before == after,
               'limits': 'Observed request/callback/transport spans on this configured fork only; no controlled performance gain or historical attribution. Concurrent summary leaves are not summed as wall time.'}
    (here / 'installed-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps({'result': receipt['result'], 'selected_requests': len(results),
                      'journey_seconds': original_receipt['elapsed_seconds']}), flush=True)

asyncio.run(main())
