from pathlib import Path
import json, hashlib, importlib.util, os, sys
private_root, private_root_id = sys.argv[1:]
base=Path('/home/ts/.cache/agent-scratch/canonical-bus-input-visibility-stage-20260930')
stage=Path('/home/ts/.local/share/agent-comms/runtime-canonical-bus-input-visibility-20260930')
assert Path(sys.prefix)==stage
receipt=json.loads((stage/'staging-receipt.json').read_text())
receipt['private_root_witness']={'root':private_root,'wire_root_id':private_root_id,'native_package':receipt['native_package'],'producer':'Einstein fresh candidate-installed Core RootProducer; root receipt /home/ts/wt/g458f/meta/root-receipt.json; zero owners/inputs/providers'}
receipt['source_admission']=json.loads((base/'source-admission.json').read_text())
for target in [base/'paired-staging-receipt.json',stage/'staging-receipt.json']:target.write_text(json.dumps(receipt,indent=2)+'\n')
pins=receipt['pins'];proof=receipt['component_source_proof'];native=Path(receipt['native_package'])
recorder_path=Path('/home/ts/wt/toad-first-ui-end-keypad-20260930/tests/tools/record_installed_tui.py')
spec=importlib.util.spec_from_file_location('stage_preflight_recorder',recorder_path);recorder=importlib.util.module_from_spec(spec);sys.modules[spec.name]=recorder;spec.loader.exec_module(recorder)
env=dict(os.environ)
for key in ('PYTHONPATH','TOAD_VIDEO_CAPTURE_TARGET','AGENT_COMMS_ROOT','AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID','AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE'):env.pop(key,None)
env['AGENT_COMMS_RUNTIME_ROOT']=str(stage/'bin');env['AGENT_COMMS_ACP_LAUNCHER']=str(stage/'bin/agent-comms-acp')
env['TOAD_VIDEO_CAPTURE_TARGET']=recorder.PrivateCapture.declared_name
env['AGENT_COMMS_ROOT']=private_root
env['AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID']=private_root_id
env['AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE']=str(native)
from agent_comms.private_nk_entrypoint import PrivateNkLaunch
PrivateNkLaunch.from_environment(Path(private_root), env).validate()
command=[str(stage/'bin/toad')]
selection=recorder.RuntimeSelection.from_environment(command,env);owner=recorder.ProcessOwner()
try:
 preflight=selection.publish_verified_stage(stage/'staging-receipt.json',owner,env,command)
 (base/'paired-runtime-preflight.json').write_text(json.dumps(preflight,indent=2)+'\n')
finally:
 cleanup=owner.cleanup();(base/'paired-probe-cleanup.json').write_text(json.dumps(cleanup,indent=2)+'\n')
assert not cleanup['remaining_owned_pids'] and not cleanup['errors']
for module,name in [('agent_comms','agent-comms'),('toad','batrachian-toad'),('textual','textual')]:assert preflight['observed']['packages'][module]['direct_url']['vcs_info']['commit_id']==pins[name]
ready={'state':'PACKAGING-READY-AWAITING-JOINT-INSTALLED-GATES','stage':str(stage),'pins':pins,'sdk':'0.12.1','native_package':str(native),'frozen_packages':68,'source_proof':proof,'runtime_preflight_pass':True,'native_full_trust':True,'metadata_published_from_same_staging_receipt':True,'owned_probe_cleanup':cleanup,'builds':1,'public_default_changes':0,'public_owner_restarts':0,'paid_calls':0,'native_inputs':0,'accepted_scope':receipt['accepted_scope'],'remaining_scope':receipt['remaining_scope']}
(base/'paired-ready-receipt.json').write_text(json.dumps(ready,indent=2)+'\n')
print(json.dumps(ready),flush=True)
