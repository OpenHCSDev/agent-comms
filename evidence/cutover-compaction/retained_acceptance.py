"""Read actual retained evidence and check the staged current admission barrier."""
import json
import sqlite3
from pathlib import Path

from agent_comms.compaction_journal import CompactionJournal, PrivateRawInput, SelectedSummaryAttempt
from agent_comms.compaction_send_admission import native_input_admitted

source = Path('/home/ts/wt/comms-acp-saved-session-startup-20260928/.artifacts/d22-integrated-current/precutover-evidence/compaction-commits.sqlite3')
staged = Path('.artifacts/retained-journal-fixed/compaction-commits.sqlite3').resolve()
before = source.read_bytes()
with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as old:
    raw = old.execute('SELECT input_id,session_file,status FROM private_raw_inputs ORDER BY input_id').fetchall()
    summaries = old.execute('SELECT operation_id,session_file,source_json,status,commit_id,decline_reason FROM selected_summary_attempts').fetchall()
journal = CompactionJournal(staged)
with journal._transaction() as db:
    assert [(r.input_id, r.session_file, r.status) for r in PrivateRawInput.select(db, order_by=('input_id',))] == raw
    current = SelectedSummaryAttempt.select(db)
assert len(current) == len(summaries) == 1
saved, row = summaries[0], current[0]
assert (row.operation_id, row.session_file, row.source_json, row.state.declared_name, row.state.decline_reason) == (saved[0], saved[1], saved[2], saved[3], saved[5])
assert not native_input_admitted(staged.parent, row.session_file)
assert source.read_bytes() == before
print(json.dumps({'raw_unknown_preserved': len(raw), 'selected_attempts_preserved': len(current), 'retained_refused_session_admitted': False, 'source_unchanged': True, 'provider_calls': 0}))
