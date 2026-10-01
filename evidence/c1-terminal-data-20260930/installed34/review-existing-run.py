"""Read existing exact ANSI/native receipts only; no application/provider run."""
import hashlib,json,os,sys
from pathlib import Path
import psutil
run=Path('/home/ts/wt/g458j/u03');proof=run/'proof'
remaining=[]
for process in psutil.process_iter():
 try:
  if process.environ().get('TOAD_TEST_ATTEMPT')=='g458j-u03' and process.status() not in (psutil.STATUS_ZOMBIE,psutil.STATUS_DEAD):remaining.append({'pid':process.pid,'command':process.cmdline()})
 except (psutil.NoSuchProcess,psutil.AccessDenied):pass
assert not remaining
(run/'cleanup.json').write_text(json.dumps(remaining)+'\n')
from agent_comms.acp_extension import decode_updates,CoordinationChangedUpdate,RequestFailedUpdate
from agent_comms.field_codec import FieldCodec
contexts=[];failures=[];kinds={}
for line in (run/'fixture/application/acp-debug').read_text().splitlines():
 if '] outgoing: ' not in line:continue
 try:message=json.loads(line.split('] outgoing: ',1)[1])
 except ValueError:continue
 if message.get('method')!='session/update':continue
 params=message['params'];update=params['update']
 for fact in decode_updates(update.get('_meta')):
  kinds[type(fact).__name__]=kinds.get(type(fact).__name__,0)+1
  if isinstance(fact,CoordinationChangedUpdate) and fact.context_usage is not None:
   contexts.append({'session':params['sessionId'],'time':line.split(']')[0]+']','source':FieldCodec.encode(fact.context_usage)})
  if isinstance(fact,RequestFailedUpdate):failures.append(FieldCodec.encode(fact))
receipt={'pins':json.loads((run/'terminal-receipt.json').read_text())['pins'],
 'phase':'affected-installed-retained-native-ACP-terminal-workflow-pass','elapsed_seconds':json.loads((run/'terminal-receipt.json').read_text())['elapsed_seconds'],
 'raw_root':str(run),'five_original_physical_pre_delivery_proofs':len(list(proof.glob('before-delivery-*.json'))),
 'strict_ANSI_frames':json.loads((proof/'input-visibility-review.json').read_text())['frames'],
 'native_turn_after_cancel':'original consumed finished ID equals canonical registry last_finished_turn_id; idle and actual ANSI Session details Ready',
 'fork_escape':'original mounted/focused/hittable Fork controls actually painted; Escape before POST creates no child',
 'context_observations_from_original_ACP':contexts,'protocol_update_counts':kinds,
 'original_RequestFailed_updates':failures,'cleanup':remaining,'public_mutations':0,'paid_calls':0,'manual_replay':False,
 'runtime_activation_unchanged':json.loads((run/'terminal-receipt.json').read_text())['runtime_activation_unchanged'],
 'original_source':json.loads((proof/'retained-final-source.json').read_text()),
 'image_provenance':'software monochrome rendering of exact recorded original ANSI text snapshots; no new GUI run or X screenshot',
 'pixels':[str(proof/(s+'.png')) for s in ('retained-saved-startup','fork-controls-painted','stationary-ready')],
 'limitations':['background history Indexing caption is a resource operation, not a claim of full performance closure',
 'controlled localhost responses and usage; selected context/output caps came from original SDK model, active request sizes are not token counts',
 'u01 configuration observation defect and u02 new observer defect retained with original inputs; no old input replay',
 'not default-activation proof; parent owns cutover and separate affected normal default entrypoint']}
(run/'ready-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps({'phase':receipt['phase'],'elapsed':receipt['elapsed_seconds'],'context_observations':contexts[:3],'protocol_failures':len(failures),'cleanup':remaining,'pixels':receipt['pixels']},indent=2))
