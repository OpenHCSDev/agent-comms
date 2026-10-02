"""Read original declaration owners; never reserve, recover, send or publish."""
from pathlib import Path
from datetime import datetime,timezone
import argparse,hashlib,json,sqlite3
from agent_comms.registration import Registration
from agent_comms.field_codec import FieldCodec
from agent_comms.compaction_records import SelectedSummaryAttempt,CompactionOperation
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.selected_source import SessionRevision
from agent_comms.input_disposition import InputDispositions
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('output',type=Path)
parser.add_argument('--root',type=Path,required=True)
parser.add_argument('--thread',required=True)
args=parser.parse_args()
root=args.root.resolve(strict=True)
name=args.thread
snapshot=Registration(root/'registry.json').snapshot()
child=snapshot.require_active(name)
source=Path(child.require_saved_session())
revision=SessionRevision.observe(str(source)).require_available()
rows=[];commits=[]
journal=root/'compaction-commits.sqlite3'
# The original journal owner fences the read-only original source first.
outcomes=CompactionJournal.snapshot(journal,str(source),child.incarnation,snapshot)
if journal.exists():
 db=sqlite3.connect(journal.absolute().as_uri()+'?mode=ro',uri=True,isolation_level=None,timeout=.25)
 try:
  db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
  for attempt in SelectedSummaryAttempt.select(db,where='session_file=?',parameters=(str(source),)):
   envelope=attempt.envelope()
   rows.append({'operation_id':attempt.operation_id,'state':attempt.state.declared_name,'source_kind':envelope.source.declared_name,'original_input_key':envelope.source.pending_input_key,'source_sha256':hashlib.sha256(attempt.source_json.encode()).hexdigest(),'retained_text_bytes':len(envelope.retained.text.encode()),'source_revision':FieldCodec.encode(envelope.source.reserved_revision)})
  for commit in CompactionOperation.select(db,where='session_file=?',parameters=(str(source),)):
   commits.append({'commit_id':commit.commit_id,'state':commit.state.declared_name,'intent_sha256':hashlib.sha256(commit.intent_json.encode()).hexdigest(),'outcome':FieldCodec.encode(commit.committed_outcome()) if commit.state.committed else None})
 finally:
  db.execute('ROLLBACK');db.close()
with InputDispositions(root/InputDispositions.filename).reading() as inputs:
 input_rows=[{'key':row.key,'kind':row.declared_name,'public_status':row.public_status,'unresolved':row.unresolved} for row in inputs.rows.values() if row.matches_owner(child.incarnation)]
report={'observed_utc':datetime.now(timezone.utc).isoformat(),'child':name,'pid':child.pid,'session_file':str(source),'session_revision':FieldCodec.encode(revision),'incarnation':FieldCodec.encode(child.incarnation),'owner':FieldCodec.encode(snapshot.owner_identity(name)),'active_turn':FieldCodec.encode(child.active_turn),'selected_summary_attempts':rows,'native_commits':commits,'compaction_outcomes':len(outcomes.outcomes),'input_rows':input_rows,'read_only':True,'sent_input':False,'provider_call':False,'recovered_or_mutated':False}
output=args.output;output.write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({'receipt':str(output),'pid':child.pid,'session_bytes':revision.native.size,'selected':[(r['operation_id'],r['state']) for r in rows],'commits':[(r['commit_id'],r['state']) for r in commits],'inputs':input_rows},indent=2))
