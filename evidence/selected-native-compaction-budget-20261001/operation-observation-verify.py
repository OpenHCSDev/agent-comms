from pathlib import Path
import hashlib, io, json, subprocess, tarfile, importlib, importlib.metadata as md, sys, os
wt=Path('/home/ts/wt/toad-receiving-reader493-scroll275-20261001')
out=wt/'.artifacts/staging-compaction499-operation-observation-20261001'; prefix=wt/'.artifacts/runtime-compaction499-operation-observation-20261001'
owner=json.loads((out/'ownership.json').read_text()); assert Path(sys.prefix)==prefix
sources=[]
for name,repo,head,dist in [
 ('agent_comms','/home/ts/wt/comms-cleanup-live-integration-20260929',owner['core'],'agent-comms'),
 ('toad',str(wt),owner['toad'],'batrachian-toad'),
 ('textual','/home/ts/wt/textual-native-subtree-retirement-20261001',owner['textual'],'textual'),
 ('textual_diff_view','/home/ts/wt/textual-diff-native-geometry-20261001','8fa7d4d0db993ea3b761c2760ca9b6a56a6251e9','textual-diff-view')]:
 mod=importlib.import_module(name); location=Path(mod.__file__).parent
 assert location.is_relative_to(prefix),str(location)
 archive=subprocess.check_output(['git','-C',repo,'archive',head,'src/'+name])
 inventory=[]
 with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
  for member in tar.getmembers():
   if not member.isfile(): continue
   relative=Path(member.name).relative_to('src')
   raw=tar.extractfile(member).read(); installed=location.parent/relative
   assert installed.is_file(),f'Missing {name}: {relative}'
   assert installed.read_bytes()==raw,f'Mismatch {name}: {relative}'
   inventory.append({'path':str(relative),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
 direct=json.loads(md.distribution(dist).read_text('direct_url.json'))
 assert direct['vcs_info']['commit_id']==head,direct
 sources.append({'module':name,'head':head,'location':str(location),'files':len(inventory),'python_files':sum(i['path'].endswith('.py') for i in inventory),'byte_equal':True,'direct_url':direct,'inventory_sha256':hashlib.sha256(json.dumps(inventory,sort_keys=True).encode()).hexdigest()})
 (out/(name+'-inventory.json')).write_text(json.dumps(inventory,indent=2)+'\n')
# Native import seam is checked without mounting or sending any input.
import toad.widgets.tool_call
import toad.acp.maintenance_ingress
import toad.acp.agent_process
from agent_comms.native_pi import _trusted_package
from agent_comms.native_package import MANIFEST,COMPACTION_HELPER,package_tree_digest
native=Path(owner['native']); trusted_cli=_trusted_package(native)
manifest=MANIFEST.read_bytes(); manifest_hash=hashlib.sha256(manifest).hexdigest()
assert manifest_hash=='5184ffa6d842fe232b370e40f9ee532696e9f83b789ab009e047c9f164026c68'
for original,target in [('stack/pi-native.sha256',MANIFEST),('stack/native-compaction-commit-child.mjs',COMPACTION_HELPER)]:
 expected=subprocess.check_output(['git','-C','/home/ts/wt/comms-cleanup-live-integration-20260929','show',owner['core']+':'+original])
 assert target.read_bytes()==expected,original
for name in ('toad','agent-comms','agent-comms-acp','agent-comms-agent','pi-comms-native'):
 p=prefix/'bin'/name; assert p.is_file() and os.access(p,os.X_OK)
 interpreter=str(prefix/'bin/python')
 # Verify the installer's complete standard POSIX interpreter declaration.
 expected=(['#!'+interpreter] if len(interpreter.encode())+3<=127 else
           ['#!/bin/sh', "'''exec' '"+interpreter+"' \"$0\" \"$@\"", "' '''"])
 assert p.read_text().splitlines()[:len(expected)]==expected,p
sdk=md.version('agent-client-protocol'); packages=sorted((d.metadata['Name'],d.version) for d in md.distributions())
assert sdk=='0.12.1' and len(packages)==69,(sdk,len(packages))
before=json.loads((out/'protected-before.json').read_text()); changed=[]
for name,digest in before.items():
 p=Path(name)
 if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=digest: changed.append(name)
assert not changed,changed[:10]
receipt={'state':'installed-source-assets-native-trust-verified; continuous journey pending','prefix':str(prefix),'sources':sources,'native_package':str(native),'native_cli':str(trusted_cli),'native_manifest':manifest_hash,'native_tree':package_tree_digest(native),'native_full_trust':True,'sdk':sdk,'package_count':len(packages),'packages':packages,'requirements_sha256':hashlib.sha256((out/'requirements.txt').read_bytes()).hexdigest(),'protected_old_prefix_files':len(before),'protected_old_prefix_files_unchanged':True,'public_install_changed':False,'new_native_build':False,'source_overlay':False,'dependency_bypass':False,'journey_owners':['Schrodinger499 canonical automatic compaction; Heisenberg284 original UI/profile; parent sole canonicalfork and child-only pin binding'],'journey_assessment':'pending'}
(out/'source-proof.json').write_text(json.dumps(receipt,indent=2)+'\n')
sys.path.insert(0, '/home/ts/wt/comms-cleanup-live-integration-20260929/tools/cutover')
from publish_retained_summary import CohortActivation
from agent_comms.field_codec import FieldCodec
from agent_comms.store_files import _atomic_write_text
activation = CohortActivation(
    stage=prefix,
    pins={'agent_comms': owner['core'], 'toad': owner['toad'], 'textual': owner['textual']},
    sdk=receipt['sdk'], textual_diff_view=owner['textual_diff_view'],
    native_package=Path(receipt['native_package']),
    state='normal69 installed source/directURLs/assets/fulltrust verified; parent activation and default gate sole owner',
    staging_receipt=out/'source-proof.json',
    bins={name: str(prefix/'bin'/name) for name in ('python', 'toad', 'agent-comms', 'agent-comms-acp', 'agent-comms-agent', 'pi-comms-native')},
    native_cli=receipt['native_cli'], native_manifest=receipt['native_manifest'], native_tree=receipt['native_tree'],
    native_configuration_note='Core499 compaction token policy and original required payload; Toad284 host publication with Textual19 damage. Native5184 source/pins normal build/fulltrust; final canonical application fork automatic compaction plus ordinary reply remains pending. No default or root changes performed.',
)
_atomic_write_text(prefix/'activation.json', json.dumps(FieldCodec.encode(activation), indent=2)+'\n')
print(json.dumps({'sources':[{k:v for k,v in s.items() if k in ('module','head','files','python_files','byte_equal')} for s in sources],'native_full_trust':True,'sdk':sdk,'package_count':len(packages),'protected_unchanged':len(before),'journey':'pending'},indent=2))
