import hashlib,json,pathlib,subprocess,sys,time,zipfile
from agent_comms.comms import Comms
from agent_comms.acp_extension import TranscriptSnapshotUpdate
from agent_comms.native_entries import TranscriptProjection
from agent_comms.native_transcript import NativeTranscript
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.transcript_events import ContextTranscript,UserTranscript,AssistantTranscript,IncomingTranscript
from agent_comms.transcript_receipts import AssignedTranscriptSource
import agent_comms
repo=pathlib.Path('/home/ts/wt/comms-context-manifest-resource-20261002')
out=repo/'.artifacts/native-stage-display'
module=pathlib.Path(agent_comms.__file__).parent
assert module.is_relative_to(repo/'.artifacts/retained-inspection521/install/runtime')
head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip()
tracked=subprocess.check_output(['git','ls-files','src/agent_comms'],cwd=repo,text=True).splitlines()
assert all((module/p.removeprefix('src/agent_comms/')).read_bytes()==(repo/p).read_bytes() for p in tracked)
root=pathlib.Path('/var/tmp/agent-comms-live-20260927-wzjtqhza')
service=Comms(root);owner=service.registry.require('openhcs-helper')
saved=pathlib.Path(owner.require_saved_session())
def sha(p):
 with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
native_before=sha(saved)
with service.bus.log.certified_read() as wire:original=wire.delivery(364)
source=AssignedTranscriptSource.for_thread(root,owner,service.bus.log)
reader=NativeTranscript(saved);matched={}
start=time.monotonic()
with service.transcripts.routes.for_session(str(saved)) as routes:
 for record in reader.reverse(saved.stat().st_size):
  if record.entry is None:continue
  if record.entry.id in ('73e6068a','9937decf'):
   events=source.native_events(record,routes,reader)
   assert events and all(isinstance(event,ContextTranscript) for event in events)
   matched[record.entry.id]=[dict(kind=event.declared_name,timestamp=event.timestamp,text_sha256=hashlib.sha256(event.text.encode()).hexdigest()) for event in events]
  if len(matched)==2:break
assert len(matched)==2
# The same actual API used by ACP saved startup and live snapshot updates.
snapshot=TranscriptSnapshotUpdate.capture(service.transcripts,owner.name)
assert any(isinstance(e,ContextTranscript) and e.timestamp==matched['73e6068a'][0]['timestamp'] for e in snapshot.page.events)
assert any(isinstance(e,ContextTranscript) and e.timestamp==matched['9937decf'][0]['timestamp'] for e in snapshot.page.events)
assert not any(isinstance(e,(UserTranscript,AssistantTranscript)) and e.timestamp in {v[0]['timestamp'] for v in matched.values()} for e in snapshot.page.events)
page=service.transcripts.thread_transcript_page(owner.name,max_messages=60,max_bytes=256*1024)
assert any(isinstance(e,IncomingTranscript) and e.source==original.message.reference and e.text==original.message.body for e in page.events)
handling=service.views.message_notifications_for_references((original.message.reference,))
owner_handling=next(n for n in handling[original.message.seq,original.message.message_id] if n.recipient==owner.name)
assert owner_handling.state=='Checked — no response'
# Reuse one existing FULL publication on this same original owner, without replay.
full=json.loads((out/'existing-full-sources.json').read_text())[0]
assert full['owner_thread']==owner.name and full['session_file']==str(saved)
full_user=full_reply=None
with service.transcripts.routes.for_session(str(saved)) as routes:
 for record in reader.reverse(saved.stat().st_size):
  entry=record.entry
  if entry is None:continue
  if entry.id==full['session_entry_id']:
   full_user=record
   assert all(isinstance(e,ContextTranscript) for e in source.native_events(record,routes,reader))
   break
  if full_reply is None and entry.final_reply:
   user=reader.input_ancestor(record)
   if user is not None and user.input_id==full['input_id']:
    raw,refs=NativeRuntimeInput.transcript_events(root,reader,record,TranscriptProjection(),full['owner_lookup'])
    assert refs and any(isinstance(e,AssistantTranscript) for e in raw)
    projected=source.native_events(record,routes,reader)
    assert not any(isinstance(e,AssistantTranscript) for e in projected)
    full_reply=dict(entry_id=entry.id,input_id=user.input_id,publication_sequences=[ref.seq for ref in refs])
assert full_user is not None and full_reply is not None
assert sha(saved)==native_before
receipt=dict(state='PASS',source_head=head,installed_python=sys.executable,module=str(module),source_assets=len(tracked),root=str(root),owner=owner.name,native_session_file=str(saved),native_file_sha256=native_before,native_file_bytes=saved.stat().st_size,native_file_unchanged=True,original_seq=original.message.seq,original_message_id=original.message.message_id,original_body_sha256=hashlib.sha256(original.message.body.encode()).hexdigest(),original_route=dict(sender=original.message.sender,target=original.message.target),original_handling=owner_handling.state,native_entry_projections=matched,original_wire_visible=True,existing_full_publication=full_reply,actual_saved_live_snapshot_api=True,elapsed_seconds=time.monotonic()-start,provider_calls=0,native_inputs=0,unknown_replays=0,public_mutations=[],scope='actual installed Core transcript reader/ACP snapshot on original public saved session. Toad source maps same ContextTranscript to disclosure; physical UI not rerecorded in this Core-only package.')
(out/'installed-saved-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps({k:receipt[k] for k in ('state','source_head','original_seq','original_handling','original_wire_visible','actual_saved_live_snapshot_api','native_file_unchanged','elapsed_seconds')}))
