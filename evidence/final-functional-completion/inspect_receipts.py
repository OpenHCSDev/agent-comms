"""Read retained acceptance evidence and live SQL without starting model work."""

import inspect
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from agent_comms.acp import CommsAgent


HERE = Path(__file__).resolve().parent
EVIDENCE = HERE.parent
STATE = Path('/home/ts/.local/state/agent-comms')
LIVE = Path('/var/tmp/agent-comms-live-20260927-wzjtqhza')


def read(path):
    return json.loads(path.read_text())


def rows(root, filename, sql, parameters=()):
    with sqlite3.connect(f'file:{root / filename}?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(sql, parameters)]


def compaction_links(root, session):
    attempts = rows(root, 'compaction-commits.sqlite3',
                    'SELECT operation_id, status, commit_id FROM selected_summary_attempts')
    commits = rows(root, 'compaction-commits.sqlite3',
                   'SELECT commit_id, status, intent_json, evidence_json FROM operations')
    native = [json.loads(line) for line in session.read_text().splitlines()]
    links = []
    for attempt in attempts:
        if attempt['status'] != 'linked':
            continue
        commit = next(row for row in commits if row['commit_id'] == attempt['commit_id'])
        assert commit['status'] == 'committed'
        intent = json.loads(commit['intent_json'])
        receipt = json.loads(commit['evidence_json'])
        assert intent['selectedSummaryOperationId'] == attempt['operation_id']
        entry = next(row for row in native if row.get('id') == receipt['entryId'])
        assert entry['type'] == 'compaction'
        links.append({'operation': attempt['operation_id'], 'commit': commit['commit_id'],
                      'native_entry': entry['id']})
    return {'linked_committed_native_entries': links,
            'other_attempts': [row for row in attempts if row['status'] != 'linked']}


def main():
    report = {'observed_at': datetime.now(timezone.utc).isoformat(),
              'provider_calls': 0, 'live_writes': 0, 'deliveries': {}}
    for sequence in (10, 12, 23, 25, 61):
        claims = rows(LIVE, 'coordination.sqlite3',
                      'SELECT recipient, audience, wake_mode, triage_verdict, disposition '
                      'FROM wake_claims WHERE wire_seq=? AND recipient IN (?,?)',
                      (sequence, 'agent-comms-ux', 'pr95-selected-pi-summary-owner'))
        inputs = rows(LIVE, 'coordination.sqlite3',
                      'SELECT n.owner_thread, n.stage, n.verdict, n.session_entry_id '
                      'FROM native_runtime_inputs n JOIN wake_claims w USING(claim_id) '
                      'WHERE w.wire_seq=?', (sequence,))
        if sequence == 10:
            assert len(claims) == 2 and not inputs
            assert all(row['disposition'] == 'passive' for row in claims)
        elif sequence == 12:
            assert claims[0]['audience'] == 'mentioned'
            assert claims[0]['disposition'] == 'completed'
            assert len(inputs) == 1 and inputs[0]['stage'] == 'full'
        else:
            assert len(claims) == 2 and len(inputs) == 3
            assert {row['disposition'] for row in claims} == {'completed', 'ignored'}
            for claim in claims:
                stages = {row['stage'] for row in inputs
                          if row['owner_thread'] == claim['recipient']}
                expected = {'triage'} if claim['disposition'] == 'ignored' else {'triage', 'full'}
                assert stages == expected
        assert all(row['session_entry_id'] for row in inputs)
        report['deliveries'][sequence] = {'claims': claims, 'native_inputs': inputs}

    repeated = read(STATE / 'pr95-real-summary-plan-20260928.json')
    assert repeated['completed'] and len(repeated['turns']) == 6
    assert len(repeated['compactions']) == 3
    for answer in (repeated['assistant_answers'][2], repeated['assistant_answers'][-1]):
        assert all(fact in answer for fact in ('ORCHID-7301', 'Thursday 14:30', 'cobalt', 'cedar'))
    report['repeated_compaction'] = compaction_links(
        Path(repeated['root']), Path(repeated['turns'][-1]['session_file']))
    assert len(report['repeated_compaction']['linked_committed_native_entries']) == 3
    report['repeated_compaction']['later_failed_queue_note'] = (
        'One reserved attempt belongs to the documented later failed queue test; '
        'it is preserved, not replayed or counted among the three successful compactions.')

    queue = read(EVIDENCE / 'r6-integration/installed-provider-queue.json')
    assert queue['verified_success']
    assert queue['queue_acceptance']['status'] == 'accepted_not_started'
    assert queue['compaction_positions'] == [7] and queue['native_start_positions'] == [8, 10]
    assert queue['assistant_answers'][-2] == 'QUEUE_ORIGINAL_OK'
    assert all(fact in queue['assistant_answers'][-1]
               for fact in ('QUEUE_FOLLOWUP_OK', 'ORCHID-7301', 'Thursday 14:30', 'cobalt', 'cedar'))
    report['queue_compaction'] = compaction_links(Path(queue['root']), Path(queue['session_file']))
    assert len(report['queue_compaction']['linked_committed_native_entries']) == 1
    assert not report['queue_compaction']['other_attempts']

    coding = read(EVIDENCE / 'functional-audit/existing-coding-transcript.json')
    assert {row['name'] for row in coding['calls']} == {'read', 'edit', 'write', 'bash'}
    assert {row['id'] for row in coding['calls']} == {row['id'] for row in coding['results']}
    assert not any(row['is_error'] for row in coding['results'])
    report['actual_provider_coding_tools'] = [row['name'] for row in coding['calls']]

    history = read(EVIDENCE / 'functional-audit/saved-session-pages.json')
    assert history['sessions'] == history['read_ok'] == 88 and history['no_inputs_sent']
    for filename in ('route-migration-corrected.json', 'attached-route-migration.json'):
        migration = read(EVIDENCE / 'r6-integration' / filename)
        assert migration['equal'] and migration['live_writes'] == 0
        assert all(table['differences'] == 0 for root in migration['roots']
                   for table in root['tables'].values())
    report['saved_sessions_readable'] = history['read_ok']
    report['route_comparisons_equal'] = True

    report['installed_acp_module'] = inspect.getfile(CommsAgent)
    report['adaptive_compaction_default'] = inspect.signature(CommsAgent).parameters[
        'adaptive_compaction_enabled'].default
    assert report['adaptive_compaction_default'] is True
    assert 'runtime-original-closure-20260928' in report['installed_acp_module']
    report['verified'] = True
    (HERE / 'receipt-observation.json').write_text(json.dumps(report, indent=2) + '\n')
    print('Verified live delivery SQL, native-linked commits, retained facts, tool results, history and installed default.')


if __name__ == '__main__':
    main()
