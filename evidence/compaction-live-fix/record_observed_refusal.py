"""Record the exact logged native refusal; never admit or replay its input."""
import hashlib
import json
from pathlib import Path

from agent_comms.active_route import read_active_route
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_states import ReservedSummary
from agent_comms.comms import Comms
from agent_comms.input_disposition import InputDispositions


root = read_active_route().root
journal = CompactionJournal(root / 'compaction-commits.sqlite3')
attempt = journal.selected_summary('7b2e8ed369834ea588f31adf5e3e5153')
source = json.loads(attempt.source_json)['source']
assert isinstance(attempt.state, ReservedSummary)
owner = Comms(root).registry.require(source['ownerName'])
assert owner.active_turn is None
assert owner.session_file == attempt.session_file
assert owner.created_at.hex() == source['ownerCreatedAt']
native = Path(attempt.session_file).stat()
assert [native.st_dev, native.st_ino, native.st_size, native.st_mtime_ns, native.st_ctime_ns] == source['reservedRevision'][0]
path = Path('/home/ts/.local/state/toad/logs/Shared_Channels_+_compact_details_preview_2026-09-28T11_25_59_946528.txt')
matches = 0
with path.open() as log:
    for line in log:
        if not line.startswith('[agent] {'):
            continue
        event = json.loads(line.removeprefix('[agent] '))
        if event.get('method') != 'session/update':
            continue
        params = event['params']
        failed = params['update'].get('_meta', {}).get('agentComms', {}).get('inputFailed')
        if params['sessionId'] == owner.name and failed and (
            failed['reason'] == 'Selected Pi declined summary (limit_exceeded); original remains unbound'
            and hashlib.sha256(failed['text'].encode()).hexdigest() == source['originalSha256']
        ):
            matches += 1
assert matches == 1
inputs = InputDispositions(root / 'input_dispositions.json')
before = inputs.read().rows[source['ingressKey']]
assert before.unresolved and before.native_id is None
journal.refuse_selected_summary(attempt.operation_id, 'limit_exceeded')
assert inputs.read().rows[source['ingressKey']] == before
print(json.dumps({'operation': attempt.operation_id, 'observed_log_matches': matches,
                  'recorded_state': journal.selected_summary(attempt.operation_id).state.declared_name,
                  'original_unknown_preserved': True, 'messages_sent': 0, 'replays': 0}))
