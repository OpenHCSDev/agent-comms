import hashlib,json,time,sys,traceback
from pathlib import Path
import agent_comms
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.field_codec import FieldCodec
from agent_comms.input_disposition import InputDocument
from agent_comms.registry_document import RegistryDocument
from agent_comms.selected_source import ManualSource,SessionRevision
from agent_comms.thread_identity import TurnId

original=Path('/home/ts/.cache/agent-scratch/mendel-original418-coverage540-20261002')
root=original/'readonly-original-store-snapshot02'
out=Path(__file__).parent
registry=FieldCodec.decode(RegistryDocument,json.loads((root/'registry.json').read_text()))
owner=registry.threads['openhcs-audit-merged-boundaries']
inputs=FieldCodec.decode(InputDocument,json.loads((root/'input_dispositions.json').read_text()))
key='acp:0d9d2784d0f34858947e138b375e83e5'
row=inputs.lookup(key)
assert type(row).__name__=='NotSentInput' and row.matches_owner(owner.incarnation)
assert row.unresolved and not row.has_native_binding
file=Path(owner.require_saved_session())
paths=[file,Path(str(file)+'.input-proof'),*sorted(root.iterdir())]
paths=[p for p in paths if p.is_file()]
def hashes():return {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
before=hashes()
revision=SessionRevision.observe(str(file)).require_available()
source=ManualSource(owner=owner.process_identity,incarnation=owner.incarnation,
 turn=TurnId('816075c342b1eda27bcb94d38e6043d2'),reserved_revision=revision)
receipt={'classification':'installed read-only original540 authenticated running-source snapshot; not stopped carry',
 'source_commit':'c2a68017f7d3280e6a30789cd7c5d907b12f959a',
 'python':sys.executable,'installed_module':agent_comms.__file__,
 'source':str(file),'source_bytes':file.stat().st_size,'snapshot_root':str(root),
 'original_incarnation':FieldCodec.encode(owner.incarnation),
 'original_input':{'key':key,'type':type(row).__name__,'admission':row.admission,
 'attention_unresolved':row.unresolved,'native_binding':row.has_native_binding},
 'input_document_rows':len(inputs.rows),'pending_input_keys':[],
 'provider_calls':0,'native_processes':0,'inputs':0,'reservations':0,
 'public_writes':0,'original_inputs_retried':0,'original_paths_rewritten':False,
 'before_sha256':before,'original_failure_receipt':str(original/'installed-original-read-receipt.json')}
started=time.monotonic()
try:
 journal=CompactionJournal(root/'compaction-commits.sqlite3')
 def read(db):
  journal.private_inputs.require_source_coverage(db,file,source,inputs,fresh=None,admission_generation=None)
  return 'accepted'
 result=CompactionJournal.observe_readonly(journal.path,read,absent='missing')
 assert result=='accepted',result
 after=hashes()
 assert before==after,'Original source/proof/store changed'
 assert SessionRevision.observe(str(file)).require_available()==revision
 assert inputs.lookup(key)==row
 receipt.update(status='SCOPED_INSTALLED_ORIGINAL_SOURCE_COVERAGE_PASS',
  original_source_coverage=result,after_sha256=after,all_original_bytes_unchanged=True,
  original_input_unchanged=True,raw_unknown_checks_retained=True)
except BaseException as error:
 receipt.update(status='FAILED_NO_REPLAY',error_type=type(error).__name__,
  error=str(error),cause_type=type(error.__cause__).__name__ if error.__cause__ else None,
  cause=str(error.__cause__) if error.__cause__ else None)
 raise
finally:
 receipt['elapsed_seconds']=time.monotonic()-started
 (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
 print(json.dumps({k:receipt[k] for k in ('status','elapsed_seconds','provider_calls','inputs')},indent=2))
