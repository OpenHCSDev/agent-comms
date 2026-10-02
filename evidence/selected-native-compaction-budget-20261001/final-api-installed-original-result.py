"""Installed original source/result qualification; no writer or provider dispatch."""
import asyncio,json,sqlite3,hashlib
from pathlib import Path
from agent_comms.compaction_records import SelectedSummaryAttempt,CompactionOperation
from agent_comms.native_compaction_request import NativeIntent,NativeSummaryPayload,NativeCommitIdentity
from agent_comms.pi_summary_payloads import SummaryFiles
from agent_comms.field_codec import FieldCodec
from agent_comms.owner_compaction_prepare import prepare_native_source
from agent_comms.owner_compaction_provider import NativeSummary
from agent_comms.selected_source import SessionRevision
from agent_comms.compaction_result import CommittedCompactionResult
root=Path('/var/tmp/agent-comms-live-20260927-wzjtqhza')
package=Path('/home/ts/wt/comms-selected-native-compaction-budget-20261001/stack/.pi-native-5184ffa6d842fe23/node_modules/@earendil-works/pi-coding-agent')
db=sqlite3.connect((root/'compaction-commits.sqlite3').as_uri()+'?mode=ro',uri=True,isolation_level=None)
try:
 db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
 attempt=SelectedSummaryAttempt.one(db,operation_id='95fe2ad984974791b588623061e799f1')
 operation=CompactionOperation.one(db,commit_id='00811c7a6dc947c79f090f4b5317ebb8')
 envelope=attempt.envelope();assert operation.represents_summary(attempt)
 intent=NativeIntent.read(operation);committed=operation.committed_outcome()
finally:db.execute('ROLLBACK');db.close()
file=Path(attempt.session_file);before=SessionRevision.observe(str(file)).require_available()
# The native journal's exact committed entry, decoded once at the external boundary.
with file.open('rb') as source:
 source.seek(max(0,file.stat().st_size-120000));source.readline()
 for raw in source:
  value=json.loads(raw)
  if value.get('id')==committed.entry_id:
   marker=FieldCodec.decode(NativeCommitIdentity,value['details']['agentCommsCommit']);assert marker==intent.identity(operation.commit_id)
   original_payload={key:value[key] for key in ('summary','tokensBefore','usage')}
   original_payload['details']={wire:value['details'][wire] for _,wire in FieldCodec._fields(SummaryFiles)}
   payload=FieldCodec.decode(NativeSummaryPayload,original_payload);break
 else:raise AssertionError('Original committed native entry absent')
assert payload.metadata_digest()==intent.metadata_digest
assert payload.payload_digest(intent.witness)==intent.payload_digest
outcome=NativeSummary(payload.summary,payload.details,payload.usage)
result=outcome.compaction_result(operation)
assert result.summary==payload.summary and result.commit_id==operation.commit_id
assert result.adaptive_result() is True
assert result.terminal_event().summary==payload.summary
assert result.prompt_response().stop_reason=='end_turn'
preparation=prepare_native_source(package,str(file),settings=envelope.settings,context_window=envelope.selected.context_window,retained_text=envelope.retained.text)
async def original_result(ready):
 ready.require_ready().witness.require_current_file(file)
 return result
projected=asyncio.run(preparation.compact_owner(original_result))
assert SessionRevision.observe(str(file)).require_available()==before
report={'installed_core':'ae660f2b2da660dfd2eab5c4b74ab29ffbf5d885','selected_attempt':attempt.operation_id,'native_commit':operation.commit_id,'native_entry':committed.entry_id,'original_payload_metadata_digests_verified':True,'preparation_member':preparation.declared_name,'result_member':projected.declared_name,'adaptive_result':projected.adaptive_result(),'original_summary_bytes':len(payload.summary.encode()),'source_revision_unchanged':True,'new_summary_or_commit':False,'provider_calls':0,'input_sent':False,'source_overlay':False,'scope':'actual installed helper and original committed result projection; not a second manual/adaptive compaction execution'}
Path(__file__).with_suffix('.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
