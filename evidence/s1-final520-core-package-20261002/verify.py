from pathlib import Path
import importlib,importlib.metadata as md,hashlib,io,json,subprocess,tarfile,sys
wt=Path('/home/ts/wt/comms-task-aware-native-bundle-20261002');out=wt/'.artifacts/s1-final520-core-20261002';owner=json.loads((out/'ownership.json').read_text());prefix=Path(owner['prefix']);assert Path(sys.prefix)==prefix
head=owner['source'];mod=importlib.import_module('agent_comms');location=Path(mod.__file__).parent;assert location.is_relative_to(prefix)
archive=subprocess.check_output(['git','-C',str(wt),'archive',head,'src/agent_comms']);inventory=[]
with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
 for m in tar.getmembers():
  if not m.isfile():continue
  relative=Path(m.name).relative_to('src/agent_comms');raw=tar.extractfile(m).read();assert (location/relative).read_bytes()==raw,relative
  inventory.append({'path':str(relative),'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)})
for source,target in [('.pi/APPEND_SYSTEM.md','APPEND_SYSTEM.md'),('stack/pi-native.sha256','pi-native.sha256'),('stack/native-compaction-commit-child.mjs','native-compaction-commit-child.mjs')]:
 raw=subprocess.check_output(['git','-C',str(wt),'show',head+':'+source]);assert (location/'_native'/target).read_bytes()==raw;inventory.append({'path':'_native/'+target,'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)})
from agent_comms.native_pi import _trusted_package
from agent_comms.native_package import MANIFEST,package_tree_digest
native=Path(owner['native']);cli=_trusted_package(native)
assert hashlib.sha256(MANIFEST.read_bytes()).hexdigest()=='0b306dac5a8b9b603949cc245f0629a9cbfc01bc4534090f2c88ca2561c1f34e'
assert md.version('agent-client-protocol')=='0.12.1'
wheel=out/'wheels/agent_comms-0.1.0-py3-none-any.whl';direct=json.loads(md.distribution('agent-comms').read_text('direct_url.json'));assert direct['url']==wheel.as_uri()
packages=sorted((d.metadata['Name'],d.version) for d in md.distributions())
receipt={'state':'READY compact normal Corewheel/SDK sourcequalified; Einstein actualS1 remains separate','prefix':str(prefix),'python':str(prefix/'bin/python'),'core':head,'wheel_sha256':hashlib.sha256(wheel.read_bytes()).hexdigest(),'core_direct_url':direct,'source_resources_byte_equal':True,'source_resource_count':len(inventory),'inventory':inventory,'sdk':'0.12.1','packages':packages,'native':str(native),'native_cli':str(cli),'manifest':hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),'tree':package_tree_digest(native),'native_full_trust':True,'dependency_strategy':'Core production+SDK projection of existing frozen69 pins, normal wheel resolver; no Toad mismatch/override/no-deps','base_requirements_sha256':hashlib.sha256(Path(owner['base_69_requirements']).read_bytes()).hexdigest(),'requirements_sha256':hashlib.sha256((out/'requirements.txt').read_bytes()).hexdigest(),'public_writes':0,'provider_calls':0,'native_build':False,'get_state_repeated':False,'source_overlay':False,'fixture_or_original_mutation':False,'acceptance':'Installedsource/package only; Einstein owns configured optionalS1 on canonical SDKfork of preserved102 source, no repeatmanual529.'}
(out/'source-proof.json').write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps({k:v for k,v in receipt.items() if k not in ('inventory','packages')},indent=2))
