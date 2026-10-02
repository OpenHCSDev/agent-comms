"""Read original seq326 witnesses; never invoke a coordinator or native writer."""

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path('/var/tmp/agent-comms-live-20260927-wzjtqhza')
INSTALLED = Path('/home/ts/wt/toad-receiving-native5-batch490-20261001/.artifacts/runtime-native5-batch490-20261001/lib/python3.14/site-packages/agent_comms')


def milliseconds(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() * 1000


def analyze(sequence=326, message_prefix="6ba2aee585ff", installed=INSTALLED):
    connection = sqlite3.connect(ROOT.joinpath('coordination.sqlite3').as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    connection.execute('BEGIN')
    try:
        claims = [dict(row) for row in connection.execute(
            'SELECT assignment_id,recipient,lifecycle,revision,accepted_at_ms,updated_at_ms '
            'FROM wake_claims WHERE wire_seq=? ORDER BY recipient', (sequence,))]
        sources = {row['input_id']: dict(row) for row in connection.execute('''
            SELECT n.input_id,n.owner_thread,n.verdict,n.session_file,n.session_entry_id,
                   w.accepted_at_ms,w.assignment_id
            FROM native_runtime_input n
            JOIN native_runtime_triage_sources s ON s.input_id=n.input_id
            JOIN json_each(s.assignment_ids) membership
            JOIN wake_claims w ON w.assignment_id=membership.value
            WHERE w.wire_seq=?
        ''', (sequence,))}
    finally:
        connection.rollback()
        connection.close()
    binding = sqlite3.connect(ROOT.joinpath('native_prompt_bindings.sqlite3').as_uri() + '?mode=ro', uri=True)
    try:
        binding_times = dict(binding.execute('SELECT input_id,bound_at_ms FROM prompt_binding'))
    finally:
        binding.close()
    results = []
    hz = os.sysconf('SC_CLK_TCK')
    for path in ROOT.joinpath('diagnostics').glob('*.requests.jsonl'):
        data = path.read_bytes()
        records = [record for line in data.splitlines()
                   if 'native' in (record := json.loads(line))]
        if not records or records[0]['native']['inputId'] not in sources:
            continue
        first, last = records[0], records[-1]
        original = sources[first['native']['inputId']]
        session_data = Path(original['session_file']).read_bytes()
        session = [json.loads(line) for line in session_data.splitlines()]
        user = next(row for row in session if row['id'] == original['session_entry_id'])
        header = session[0]
        results.append({
            'owner': original['owner_thread'], 'input_id': original['input_id'],
            'assignment_id': original['assignment_id'], 'verdict': original['verdict'],
            'turn': first['turn'], 'native_process': first['native_process'],
            'request_file': path.name, 'request_sha256': hashlib.sha256(data).hexdigest(),
            'session_sha256': hashlib.sha256(session_data).hexdigest(),
            'session_id': header['id'], 'session_entry_id': user['id'],
            'accepted_at_ms': original['accepted_at_ms'],
            'bound_at_ms': binding_times[original['input_id']],
            'user_at_ms': milliseconds(user['timestamp']),
            'native_preparing_at_ms': first['native']['startedAtMs'],
            'native_finished_at_ms': last['native']['observedAtMs'],
            'claim_to_native_preparing_ms': first['native']['startedAtMs'] - original['accepted_at_ms'],
            'header_to_original_user_ms': round(milliseconds(user['timestamp']) - milliseconds(header['timestamp']), 3),
            'child_birth_to_native_preparing_ms': round(int(first['native']['monotonicNs']) / 1e6 - first['native_process']['start_time'] * 1000 / hz, 3),
            'model_elapsed_ms': last['native']['elapsedMs'],
            'native_callbacks_ms': last['native']['callbackMs'],
            'maximum_observation_lag_ms': max((row['recorded_monotonic_ns'] - int(row['native']['monotonicNs'])) / 1e6 for row in records),
            'stages': [{'stage': row['native']['stage'], 'elapsed_ms': row['native']['elapsedMs']} for row in records],
        })
    names = ('coordinated_runtime.py', 'selected_participant.py', 'private_send_admission.py',
             'native_prompt_send.py', 'tracked_turn.py', 'native_startup.py',
             'coordination_response.py', 'compaction_private_inputs.py', 'backend.py')
    return {
        'source_wire_seq': sequence, 'source_message_id_prefix': message_prefix,
        'kind': 'readonly_original_witness_join', 'claims': claims,
        'requests': sorted(results, key=lambda row: row['native_preparing_at_ms']),
        'installed_source_sha256': {name: hashlib.sha256(installed.joinpath(name).read_bytes()).hexdigest() for name in names},
        'limits': [
            'No historical get_state-response, prompt-writer-grant, or physical-lock wait timestamps were retained.',
            'Header-to-user precedes model preparation but cannot distinguish parent admission wait from native pre-user handling.',
            'Recorded observation lag includes queued consumption and Python publication; it is not a pure IPC or lock duration.',
            'Kernel process birth has scheduler tick resolution; native elapsed measurements use the original monotonic clock.',
            'No public writer, native input, replay, restart, repair, or provider invocation is performed.',
        ],
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seq', type=int, default=326)
    parser.add_argument('--message-prefix', default='6ba2aee585ff')
    parser.add_argument('--installed', type=Path, default=INSTALLED)
    parser.add_argument('--output', type=Path, default=Path(__file__).with_name('receipt.json'))
    arguments = parser.parse_args()
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(analyze(arguments.seq, arguments.message_prefix,
        arguments.installed), indent=2) + '\n')
