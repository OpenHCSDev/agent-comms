from pathlib import Path
import json, hashlib, subprocess, importlib, importlib.metadata as md, importlib.util, os, sys
private_root, private_root_id = sys.argv[1:]
base=Path('/home/ts/.cache/agent-scratch/canonical-bus-input-visibility-stage-20260930')
stage=Path('/home/ts/.local/share/agent-comms/runtime-canonical-bus-input-visibility-20260930')
assert Path(sys.prefix)==stage
pins={'agent-comms':'ea8aa4eaee1ce3eed9aef19cd44010b592f83b26','batrachian-toad':'c0750340f069c90be2c65bb8f8d7f8affd32d02a','textual':'2e49cb838af44d69aa5a6d76b2a1d74cfbe67347'}
sources={'agent-comms':('/home/ts/wt/comms428-native-wait-custody-20260930','src/agent_comms','agent_comms'),'batrachian-toad':('/home/ts/wt/toad-fork-task-editor-20260930','src/toad','toad')}
proof={};provenance={}
for name,pin in pins.items():
 d=md.distribution(name);direct=json.loads(d.read_text('direct_url.json'));assert direct['vcs_info']['commit_id']==pin and not direct.get('dir_info',{}).get('editable',False)
 provenance[name]=direct
 if name in sources:
  repo,prefix,module=sources[name];origin=Path(importlib.util.find_spec(module).origin).parent;assert origin.is_relative_to(stage)
  paths=subprocess.check_output(['git','ls-tree','-r','--name-only',pin,'--',prefix],cwd=repo,text=True).splitlines()
  count=0
  for path in paths:
   if not path.endswith('.py'):continue
   original=subprocess.check_output(['git','show',pin+':'+path],cwd=repo)
   installed=origin/Path(path).relative_to(prefix)
   assert installed.read_bytes()==original,path
   count+=1
  proof[name]={'pin':pin,'python_files_equal':count,'noneditable':True,'origin':str(origin),'source':'exact merged Git tree'}
assert md.version('agent-client-protocol')=='0.12.1'
from agent_comms.native_pi import _trusted_package
from agent_comms.native_package import MANIFEST
native=Path('/home/ts/.local/share/agent-comms/native-current-593b978a717ae8f6/node_modules/@earendil-works/pi-coding-agent')
_trusted_package(native)
assert hashlib.sha256(MANIFEST.read_bytes()).hexdigest()=='593b978a717ae8f6ab4300a1a107b615c8ced52e767ef4cad0596f827041a218'
modules=['agent_comms.private_nk_entrypoint','agent_comms.active_route','agent_comms.cohort_foreground','agent_comms.response_conversation','agent_comms.coordination_response','toad.widgets.conversation','toad.widgets.viewport_body','toad.widgets.presentation_window','toad.screens.comms']
for name in modules:importlib.import_module(name)
receipt={'owner':'Schrodinger250finalstage','stage':str(stage),'pins':pins,'sdk':'0.12.1','native_package':str(native),'native_manifest_sha256':hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),'frozen_package_count':68,'only_changed_requirements':['agent-comms','batrachian-toad'],'baseline':'/home/ts/.local/share/agent-comms/runtime-all-merged-20260930','requirements_sha256':hashlib.sha256((base/'paired-requirements.txt').read_bytes()).hexdigest(),'component_source_proof':proof,'component_provenance':provenance,'package_compatibility':'normal uv pip check passed68','affected_imports':modules,'full_native_trust':True,'build_count':1,'parent_activation_required':True,'defaults_changed':False,'owners_restarted':False,'provider_calls':0,'native_inputs':0,'public_root_loaded':False,'private_root_witness':{'root':private_root,'wire_root_id':private_root_id,'native_package':str(native),'producer':'Einstein owned fresh final Core458 RootProducer, no native inputs/owners'},'actual_gate_owner':'Einstein231','accepted_scope':'Packaging/source provenance/full native trust admission ONLY. Includes428 exact merged fixture ancestry,461 observation owner,460 activity3/display2,251 input visibility and252 answer source. Einstein joint installed native/ACP/UI gate then Schrodinger bus/hotDM/IRC gate required before readiness/publication.','remaining_scope':'Not installed workflow Ready. Joint firstfork/queue/cancel then bus/DM/IRC original-once/handling/immediateSend/idle/reopen gate pending.461 runtime_info dated observation reset and460 disposable activity/display cutover must use existing quiet custody before target readers; public effects parent-owned. No current default or owner changes.'}
for path in [base/'paired-staging-receipt.json',stage/'staging-receipt.json']:path.write_text(json.dumps(receipt,indent=2)+'\n')
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
