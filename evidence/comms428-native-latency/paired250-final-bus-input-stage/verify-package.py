from pathlib import Path
import json, hashlib, subprocess, importlib, importlib.metadata as md, importlib.util, os, sys
private_root, private_root_id = "pending-Einstein-fresh-root", "pending"
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

print(json.dumps({"state":"package-source-native-trust-pass","stage":str(stage),"proof":proof,"native_full_trust":True}),flush=True)
