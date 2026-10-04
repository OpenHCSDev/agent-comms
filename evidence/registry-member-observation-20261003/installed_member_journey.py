"""One installed CLI/member lifetime; no native input or public root."""
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import time
import tomllib
import zipfile
from dataclasses import fields
from pathlib import Path

import agent_comms
from agent_comms.comms import Comms
from agent_comms.errors import RelationViolationError, UnregisteredThreadError
from agent_comms.field_codec import FieldCodec
from agent_comms.registry_document import RegistryDocument, RegistrySnapshot
from agent_comms.registry_provenance import RegistryProvenance
from agent_comms.store_files import _atomic_write_text

checkout = Path(sys.argv[1]).resolve()
root = Path(sys.argv[2]).resolve()
assert not root.exists(), 'fresh owned fixture required'
root.mkdir(parents=True, mode=0o700)
wheel = checkout / '.artifacts/registry-member605-wheels/agent_comms-0.1.0-py3-none-any.whl'
package = Path(agent_comms.__file__).parent
prefix = Path(sys.executable).parent.parent
assert package.is_relative_to(prefix)
source_paths = subprocess.check_output(['git','ls-files','src/agent_comms'],cwd=checkout,text=True).splitlines()
declared_files = {relative.removeprefix('src/'): checkout/relative for relative in source_paths}
build = tomllib.loads((checkout/'pyproject.toml').read_text())
for source, destination in build['tool']['hatch']['build']['targets']['wheel']['force-include'].items():
    assert destination not in declared_files
    declared_files[destination] = checkout/source
with zipfile.ZipFile(wheel) as bundle:
    members = {name:bundle.read(name) for name in bundle.namelist() if name.startswith('agent_comms/') and not name.endswith('/')}
    assert members.keys() == declared_files.keys()
    for name, source in declared_files.items():
        assert members[name] == source.read_bytes() == (package.parent/name).read_bytes(), name
    assert {str(p.relative_to(package.parent)) for p in package.rglob('*') if p.is_file() and '__pycache__' not in p.parts} == members.keys()
proof = {'python':sys.executable,'core_head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=checkout,text=True).strip(),'installed_package':str(package),'python_files':sum(p.endswith('.py') for p in members),'package_files':len(members),'wheel_sha256':hashlib.sha256(wheel.read_bytes()).hexdigest(),'direct_url':json.loads(importlib.metadata.distribution('agent-comms').read_text('direct_url.json'))}
clean_env={k:v for k,v in os.environ.items() if not k.startswith(('AGENT_COMMS_','PI_','PYTHONPATH'))}
commands=[]
def cli(*args, expect=0):
    result=subprocess.run([str(prefix/'bin/agent-comms'),'--root',str(root),*args],cwd=root,env=clean_env,capture_output=True,text=True,timeout=15)
    commands.append({'args':list(args),'exit':result.returncode,'stdout':result.stdout,'stderr':result.stderr})
    assert result.returncode==expect,commands[-1]
    return json.loads(result.stdout) if expect==0 else None
started=time.monotonic()
cli('register','--name','original','--worktree',str(root))
comms=Comms(root)
registry=comms.registry
original=registry.require('original')
assert registry.status('original').running
assert cli('thread','--name','original','--no-pending')['created_at']==original.created_at
registry.rename('original','renamed')
assert registry.require('original').name=='renamed'
assert registry.status('original').running
assert registry.last_seen('original')>0
cut=registry.snapshot()
provenance=RegistryProvenance.capture(cut)
assert provenance.require('original').name=='renamed'
assert not hasattr(provenance,'status')
assert provenance.canonical_name('unregistered')=='unregistered'
assert cli('thread','--name','original','--no-pending')['name']=='renamed'
with registry.store.reading() as document:
    assert document.require('original') is cut.require('original')
    assert document.status('original') is cut.status('original')
    assert document.seen_at('original')==cut.seen_at('original')
    encoded=FieldCodec.encode(document)
    assert RegistryDocument.from_wire(encoded)==document
assert [f.name for f in fields(RegistryDocument)]==['threads','statuses','last_seen','aliases','owners','admissions']
assert [f.name for f in fields(RegistrySnapshot)]==['threads','aliases','statuses','last_seen','owner_generations','admission_generations']
registry.unregister('original')
assert registry.status('original').stopped and cut.status('original').running
assert cli('thread','--name','original','--no-pending')['status']=='stopped'
try:
    registry.store.read().require_active('original')
except RelationViolationError:
    pass
else:
    raise AssertionError('stopped member accepted as active')
registry.archive('original')
registry.begin_delete('original')
registry.remove('original')
for read in (registry.require,registry.status,registry.last_seen):
    try:read('original')
    except UnregisteredThreadError:pass
    else:raise AssertionError('removed member accepted')
assert cut.require('original').incarnation.created_at==original.created_at
cli('thread','--name','original','--no-pending',expect=1)
retained=RegistryDocument()
assert retained.restore_stopped(cut,('renamed',))==('renamed',)
assert retained.seen_at('renamed')==cut.seen_at('renamed')
# The original strict owner rejects malformed external replacement, never stale success.
registry.store.replace(retained)
raw=FieldCodec.encode(retained)
saved=registry.store.path.read_bytes()
raw['last_seen'].pop('renamed')
_atomic_write_text(registry.store.path,json.dumps(raw))
try:registry.last_seen('renamed')
except RelationViolationError:pass
else:raise AssertionError('missing date became fabricated freshness')
_atomic_write_text(registry.store.path,saved.decode())
assert registry.last_seen('renamed')==cut.seen_at('renamed')
receipt={'state':'SCOPED_INSTALLED_REGISTRY_MEMBER_CLI_PASS','elapsed_seconds':time.monotonic()-started,'root':str(root),'proof':proof,'cli':commands,'retained_cut_preserved':True,'strict_missing_timestamp_refused':True,'native_launches':0,'provider_calls':0,'public_inputs':0,'original_source_mutations':0,'owned_processes_remaining':[],'scope':'installed CLI/private registration/status/alias/removal/restoration; no native/provider/physical UI or latency gain'}
(root/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps({'state':receipt['state'],'elapsed_seconds':receipt['elapsed_seconds'],'package_files':proof['package_files'],'python_files':proof['python_files'],'root':str(root)}))
