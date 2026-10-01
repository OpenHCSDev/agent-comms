from pathlib import Path
import json,hashlib
from task_envelope_promotion import AuthoredTaskMemberPromotion
from agent_comms.wire_record import WireRecord
from agent_comms.bus_publication import PRIVATE_WIRE_FIELD
w=Path.cwd();e=w/'evidence/retained-task-facts-s2-followup-20261001'
p=json.loads((e/'carry-original39.json').read_text());results=[];out=[]
for entry in p['rows']:
    raw=json.loads(entry['raw']);public=entry['public']
    changed=public is not None and 'decision' in public
    target=AuthoredTaskMemberPromotion.record(raw,public,p['root_id']) if changed else raw
    record=WireRecord.from_wire(target,p['root_id'])
    if changed:
        restored=dict(record.message.to_wire());restored['decision']=restored.pop('task')
        assert restored==public
        assert target[PRIVATE_WIRE_FIELD]!=raw[PRIVATE_WIRE_FIELD]
        copied=dict(target);copied[PRIVATE_WIRE_FIELD]=raw[PRIVATE_WIRE_FIELD]
        try: WireRecord.from_wire(copied,p['root_id'])
        except ValueError: pass
        else: raise AssertionError('Copied old envelope certificate was admitted')
        results.append({'reference':{'seq':record.message.seq,'message_id':record.message.message_id},'kind':record.message.task.declared_name,'new_original_envelope_attested':True,'copied_signature_refused':True})
    out.append(target)
r={'state':'ORIGINAL_CERTIFIED_PREIMAGE_TARGET_ATTESTATION_PASS','original_rows':len(p['rows']),'changed':results,'target_rows_decoded':len(out),'original_packet_sha256':hashlib.sha256((e/'carry-original39.json').read_bytes()).hexdigest(),'original_root_changed':False,'target_published':False,'installed_carry':False,'provider_calls':0}
(e/'carry-recertification41.json').write_text(json.dumps(r,indent=2)+'\n');print(json.dumps(r))
