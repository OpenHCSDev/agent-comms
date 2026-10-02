import hashlib,json,sqlite3,sys
from pathlib import Path
from agent_comms.comms import Comms
from agent_comms.coordinator import Coordination
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.coordination_tables.publications import PublicationReceipts
from agent_comms.native_runtime_input import NativeRuntimeInput,CurrentNativeCursor
from agent_comms.native_input_record import TriageNativeExecution,FullNativeExecution
from agent_comms.selected_triage import FullSelectedTriage
from agent_comms.historical_native_inputs import read_historical_native_inputs
from agent_comms.native_entries import NativeEntry
from agent_comms.acp_extension import decode_updates,CursorAdvancedUpdate,VerifiedCursorObservation
from agent_comms.field_codec import FieldCodec
from agent_comms.coordination_cohort import _receipt_matches

stage=Path('/home/ts/wt/b509c01'); root=stage/'wire'
raw=stage/'receipt.json'; original=json.loads(raw.read_text())
service=Comms(root); marker=service.bus.log.read_metadata_unlocked()
sequences=original['original_sequences']; names={r['private_owner'] for r in original['settings']}
protected=[root/'bus.jsonl',root/'coordination.sqlite3',root/'registry.json',raw]
protected += list((root/'native-sessions').rglob('*.jsonl'))+list((root/'native-sessions').rglob('*.input-proof'))
digests={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
originals=tuple(m for m in service.bus.log.full_history() if m.seq in sequences)
assert len(originals)==3
packets=json.loads((stage/'acp-observer.json').read_text())
with Coordination(root/'coordination.sqlite3') as store:
 db=store.session._connection; db.execute('PRAGMA query_only=ON')
 claims=WakeAssignment.select(db,where='wire_seq IN (?,?,?)',parameters=tuple(sequences),order_by=('wire_seq',))
 assert len(claims)==9 and all(c.lifecycle.completed for c in claims)
 rows=NativeRuntimeInput.select(db); by_input={r.input_id:r for r in rows}
 original_ids={c.assignment_id for c in claims}
 members={r.input_id:r.execution.source_assignment_ids(db,r.input_id) for r in rows}
 inputs=tuple(r for r in rows if original_ids.intersection(members[r.input_id]))
 triage=tuple(r for r in inputs if r.reference_stage is TriageNativeExecution)
 full=tuple(r for r in inputs if r.reference_stage is FullNativeExecution)
 assert len(triage)==len(full)==3 and all(r.verdict is FullSelectedTriage for r in triage)
 for r in inputs:
  expected=tuple(c.assignment_id for c in claims if c.recipient==r.owner_thread)
  assert members[r.input_id]==expected
  with NativeEntry.open_evidence(Path(r.session_file)) as evidence: _,entries=evidence.observe()
  user=NativeEntry.tracked_users(entries)[r.input_id]
  assert all(a in user.message.text for a in expected)
  assert all(json.dumps(m.body,ensure_ascii=True) in user.message.text for m in originals)
 receipts={}
 for m in originals:
  initial=service.bus.log.read_delivery_cohort(marker.root_id,m.seq)
  sealed=_receipt_matches(db,initial)
  assert {r.assignment_id for r in sealed.assignments}=={c.assignment_id for c in claims if c.wire_seq==m.seq}
 for c in claims:
  history=read_historical_native_inputs(store,wire_root_id=marker.root_id,recipient_lookup=c.recipient_lookup,source_seq=c.wire_seq)
  assert len(history)==2 and all(h.expected_prompt_equality_established for h in history)
  receipt,=PublicationReceipts.select(db,where='execution_id=?',parameters=(c.lifecycle.execution_id,))
  reply=service.bus.log.message_by_id(receipt.message_id)
  assert receipt.exact_target=='#openhcs' and reply.sender==c.recipient and '12' in reply.body
  receipts[receipt.execution_id]=receipt
 cursors=CurrentNativeCursor.select(db); assert len(cursors)==3
 assert {c.owner_thread for c in cursors}==names and all(c.covered_seq>=max(sequences) for c in cursors)
 observed={}
 for p in packets:
  for fact in decode_updates(p['update'].get('_meta')):
   if isinstance(fact,CursorAdvancedUpdate) and isinstance(fact.envelope.observation,VerifiedCursorObservation):
    c=fact.envelope.observation.cursor
    if c.owner_thread in names and c.covered_seq>=max(sequences):
     native=by_input[c.input_id]
     assert c.owner_identity==native.owner_identity and c.reference==native.reference
     assert native.sent_owner_admission_generation.matches(c.owner_admission_generation)
     assert fact.envelope.scope.owner_pid==service.registry.require(c.owner_thread).pid
     observed[c.owner_thread]=c
 assert set(observed)==names
 assert all(observed[c.owner_thread]==c for c in cursors)
 assert len(receipts)==3
assert original['all_owned_workers_retired'] and original['configured_sources_unchanged']
assert original['failure']=='TimeoutError: ' and not list((root/'diagnostics').glob('*.json'))
assert len(original['bus_reads'])==180 and original['retained_bus_bytes']==2951312
assert any(len({r['owner'] for r in t['dispatched_unproven_inputs']})==3 for t in json.loads((stage/'native-overlap-timeline.json').read_text()))
assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==digest for p,digest in digests.items())
report={'qualification':'original channel membership/native proof/response/history/current ACP coverage verified read-only; raw driver timeout retained',
 'core':'ca4bcfa5efdcde807a4da26309222760939aefc1','toad':'26b425a3b68ab02d38ef842d75fc587c267c6ea1','native_manifest':'5184ffa6d842fe232b370e40f9ee532696e9f83b789ab009e047c9f164026c68',
 'installed_python':sys.executable,'stage':str(stage),'raw_receipt_sha256':digests[str(raw)],'raw_failure':original['failure'],
 'original_claims_completed':9,'original_triage_full_decisions':3,'original_full_native_proofs':3,'original_sealed_receipts':3,'channel_reply_receipts':3,
 'all_original_native_prompt_memberships':True,'all_historical_native_prompt_and_context_proofs':True,
 'actual_acp_current_cursor_stages':[c.stage.declared_name for c in cursors],'current_cursor_coverage':[c.covered_seq for c in cursors],
 'actual_acp_cursor_matches_current_original_proof':True,'original_last_sequence':max(sequences),'bus_reads':len(original['bus_reads']),
 'retained_bus_bytes':original['retained_bus_bytes'],'overlapping_original_native_dispatch':True,'diagnostics':0,
 'protected_files':len(digests),'protected_files_unchanged':True,'configured_sources_unchanged':True,'owned_workers_retired':True,
 'provider_repeat':0,'public_inputs':0,'public_mutations':0,
 'cause':'Fixture required a past FULL cursor indefinitely. NativeSourceCursor advances monotonically with later proven peer-reply TRIAGE; original FULL proof remains with historical inputs and publication receipts.',
 'limits':'No complete Native6/saved-session continuation, full-compaction/performance or physical TUI claim. Raw driver exit was 1, not retroactively changed.'}
Path('.artifacts/configured509-qualified01.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
