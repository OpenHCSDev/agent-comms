"""Loaded-history installed proof through original InstalledSource and native trust owners."""
from pathlib import Path
import sys, os, json, hashlib, stat, subprocess, io, tarfile, zipfile
import importlib, importlib.metadata as metadata

sha = lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
grant_path = Path(sys.argv[1]); grant_sha = sys.argv[2]
assert sha(grant_path) == grant_sha
grant = json.loads(grant_path.read_bytes())
lifecycle_raw = Path(grant['lifecycle']).read_bytes()
lifecycle = json.loads(lifecycle_raw)
assert lifecycle['issued_sha256'] == grant_sha
assert lifecycle['proof_execution_authorized'] and lifecycle['proof_attempts_consumed'] == 0
assert lifecycle['native_READ_authorized'] and not lifecycle['native_EXEC_authorized']
assert sha(grant['proposal']['path']) == grant['proposal']['sha256']
proposal = json.loads(Path(grant['proposal']['path']).read_bytes())
prefix = Path(grant['prefix']); output = Path(grant['owned_output'])
assert Path(sys.prefix) == prefix == Path(proposal['holder']['existing_nonlive_prefix'])
assert output == Path(proposal['command']['owned_output'])
assert not os.environ.get('PYTHONPATH') and os.environ.get('PYTHONDONTWRITEBYTECODE') == '1'
assessment_record = proposal['holder']['assessment']
assert sha(assessment_record['path']) == assessment_record['sha256']
assessment = json.loads(Path(assessment_record['path']).read_bytes())
keeper_source = assessment['fresh_preimage']
assert sha(keeper_source['path']) == keeper_source['sha256']
keepers = json.loads(Path(keeper_source['path']).read_bytes())
read_grant = lifecycle['matching_native_READ']
assert sha(read_grant['path']) == read_grant['sha256']
read_receipt = json.loads(Path(read_grant['path']).read_bytes())
assert Path(read_receipt['holder_grant']) == grant_path
assert read_receipt['holder_grant_sha256'] == grant_sha
assert Path(read_receipt['personally_read_lifecycle']) == Path(grant['lifecycle'])
assert read_receipt['native_READ_authorized'] is True
assert read_receipt['native_EXEC_authorized'] is False
assert read_receipt['matching_native'] == {
    'package': proposal['native']['native_package'],
    'manifest': proposal['native']['native_manifest'],
    'tree': proposal['native']['native_tree'],
}
# The immutable holder binds the proposal hash; Bohr's same lifecycle binds
# the subsequently issued Sch READ receipt. No receipt hash points backwards
# into the immutable holder that the receipt itself must authenticate.
assert json.loads((output/'stage-terminal.json').read_bytes())['exit_code'] == 0
proof_path = output/'candidate-source-proof.json'; dto_path = output/'candidate-activation.json'
new_names = ['agent_comms-inventory.json','toad-inventory.json','textual-inventory.json','refactor_audit-inventory.json','textual_diff_view-inventory.json','wheel-installed-readback.json','forced-native-assets.json','metadata-origin-readback.json','retained-wheel-install.txt','unchanged-keepers-after.json','native-full-trust.json','proof-result.json']
assert all(not p.exists() for p in (proof_path, dto_path, *(output/name for name in new_names)))

def write(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, indent=2); stream.write('\n')

def check(record):
    path = Path(record['path']); info = path.lstat()
    assert stat.S_IMODE(info.st_mode) == record['mode'], path
    if record['kind'] == 'file':
        assert stat.S_ISREG(info.st_mode) and sha(path) == record['sha256'], path
    elif record['kind'] in ('symlink','link'):
        assert stat.S_ISLNK(info.st_mode) and os.readlink(path) == record.get('target', record.get('readlink', record.get('link'))), path
    else:
        raise AssertionError(record)

for record in keepers['unchanged_other66_unique_records']: check(record)
for path, record in keepers['protected_originals'].items(): check({'path':path,'kind':'file',**record})
tools = Path('/home/ts/wt/comms-configured-continuous-producer-20261006/tools/cutover')
sys.path.insert(0, str(tools))
from agent_comms.field_codec import FieldCodec
from publish_retained_summary import InstalledSource, InstalledSourceProof, CohortActivation, ReviewedArtifact, VcsPackageDirectUrl, ArchivePackageDirectUrl

for record in keepers['additional_original_environment_nodes']: check(record)
assert sha(prefix/'activation.json') == keepers['PREFIXactivation_sha256']
wheel_specs = {row['module']: row for row in proposal['package_sources']}
heads = {module:row['head'] for module,row in wheel_specs.items()}
repos = {module:(row['repo'],row['source_root']) for module,row in wheel_specs.items()}
artifacts = tuple(ReviewedArtifact(Path(row['path']), row['sha256']) for row in wheel_specs.values())
for artifact in artifacts: artifact.require_original()
sources = []; forced = []; wheel_readback = []
for module, spec in wheel_specs.items():
    location = Path(importlib.import_module(module).__file__).resolve().parent
    assert location.is_relative_to(prefix.resolve()), location
    repo, root = repos[module]; head = heads[module]
    archive = subprocess.check_output(['git','-C',repo,'archive',head,root])
    committed = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
        for entry in tree.getmembers():
            if entry.isfile():
                name = (Path(module)/Path(entry.name).relative_to(root)).as_posix()
                committed[name] = tree.extractfile(entry).read()
    if module == 'agent_comms':
        for original, member in [('stack/pi-native.sha256','agent_comms/_native/pi-native.sha256'),('stack/native-compaction-commit-child.mjs','agent_comms/_native/native-compaction-commit-child.mjs'),('.pi/APPEND_SYSTEM.md','agent_comms/_native/APPEND_SYSTEM.md')]:
            raw = subprocess.check_output(['git','-C',repo,'show',head+':'+original])
            committed[member] = raw
            forced.append({'source':original,'member':member,'sha256':hashlib.sha256(raw).hexdigest()})
    actual = {module+'/'+path.relative_to(location).as_posix() for path in location.rglob('*') if path.is_file() and '__pycache__' not in path.parts}
    inventory = []
    with zipfile.ZipFile(spec['path']) as wheel:
        names = {name for name in wheel.namelist() if name.startswith(module+'/') and not name.endswith('/')}
        assert actual == names == set(committed), (module,actual-names,names-actual,names-set(committed))
        for name in sorted(names):
            installed = location.parent/name; raw = installed.read_bytes()
            assert raw == wheel.read(name) == committed[name], name
            inventory.append({'path':name,'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'Git_ZIP_installed_equal':True})
    assert len(inventory) == spec['assets']
    inventory_path = output/(module+'-inventory.json'); write(inventory_path, inventory)
    distribution = metadata.distribution(spec['package'])
    direct = json.loads(distribution.read_text('direct_url.json'))
    origin = FieldCodec.decode(VcsPackageDirectUrl|ArchivePackageDirectUrl, direct)
    source = InstalledSource(module,head,str(location),len(inventory),sum(row['path'].endswith('.py') for row in inventory),True,origin,sha(inventory_path))
    source.require_package(module,location,direct,artifacts)
    sources.append(source)
    wheel_readback.append({'module':module,'head':head,'wheel':spec['path'],'sha256':spec['sha256'],'assets':len(inventory),'no_extra_assets':True,'Git_ZIP_installed_equal':True})
assert sum(source.files for source in sources) == 953
packages = sorted((distribution.metadata['Name'],distribution.version) for distribution in metadata.distributions())
assert len(packages) == 69 and packages == sorted(keepers['normal_distributions'].items())
origins = {distribution.metadata['Name']:distribution.read_text('direct_url.json') for distribution in metadata.distributions()}
for name, old in keepers['original69_origins'].items():
    if name in ('agent-comms', 'batrachian-toad', 'textual'):
        spec = next(row for row in wheel_specs.values() if row['package'] == name)
        assert json.loads(origins[name])['url'] == Path(spec['path']).as_uri()
    elif old is None:
        assert origins[name] is None, name
    else:
        assert sha(old['path']) == old['sha256'], name
        assert origins[name] == Path(old['path']).read_text() and json.loads(origins[name]) == old['contents'], name
requirements = ''.join(name+'=='+version+'\n' for name,version in packages)
with (output/'retained-wheel-install.txt').open('x') as stream: stream.write(requirements)
write(output/'metadata-origin-readback.json',{'packages':packages,'origins':origins,'other66_raw_origins_unchanged':True})
write(output/'wheel-installed-readback.json',wheel_readback); write(output/'forced-native-assets.json',forced)
write(output/'unchanged-keepers-after.json',{'unchanged_other66_records':len(keepers['unchanged_other66_unique_records']),'protected_originals':len(keepers['protected_originals']),'all_hash_mode_link_equal':True,'original_PREFIXactivation_unchanged':True})
manifest = next(row['sha256'] for row in forced if row['source']=='stack/pi-native.sha256')
resource = Path(next(source.location for source in sources if source.module=='agent_comms'))/'_native/pi-native.sha256'
tree = next(line.split()[-1] for line in resource.read_text().splitlines() if line.startswith('# agent-comms-native-tree-v1 '))
native = Path(proposal['native']['native_package'])
assert manifest == proposal['native']['native_manifest'] and tree == proposal['native']['native_tree']
from agent_comms.native_package import verify_native_package
verify_native_package(native)
write(output/'native-full-trust.json',{'native_package':str(native),'manifest':manifest,'tree':tree,'native_full_trust':True,'owner':'installed agent_comms.native_package.verify_native_package','matching_native_READ':read_grant,'native_execution':0})
proof = InstalledSourceProof('PASS package/source/origin and original native FullTrust; loaded-history control held',prefix,tuple(sources),native,str(native/'dist/cli.js'),manifest,tree,True,metadata.version('agent-client-protocol'),69,tuple(packages),sha(output/'retained-wheel-install.txt'),len(keepers['protected_originals']),True,False,False,False,False,(), 'Loaded4/16/32/64 behavior remains unrun. FullTrust does not grant native execution.',artifacts)
dto = CohortActivation(prefix,{source.module:source.head for source in sources if source.module!='textual_diff_view'},proof.sdk,heads['textual_diff_view'],native,'Own private loaded-history DTO; original PREFIX activation immutable',proof_path,{name:str(prefix/'bin'/name) for name in ('python','toad','agent-comms','agent-comms-acp','agent-comms-agent','pi-comms-native')},proof.native_cli,manifest,tree,'Original4b FullTrust/source proof only; actual native execution requires separate new SchEXEC and Bohrrelease.')
proof.require_activation(dto)
write(proof_path,FieldCodec.encode(proof)); write(dto_path,FieldCodec.encode(dto))
birth = int(Path(f'/proc/{os.getpid()}/stat').read_text().rsplit(')',1)[1].split()[19])
write(output/'proof-result.json',{'state':'PASS','proof':str(proof_path),'proof_sha256':sha(proof_path),'DTO':str(dto_path),'DTO_sha256':sha(dto_path),'installed_assets':953,'packages':69,'protected':len(keepers['protected_originals']),'other66_records':len(keepers['unchanged_other66_unique_records']),'native_full_trust':True,'native_artifact_reads':1,'native_execution':0,'provider_inputs':0,'public_operations':0,'controller':{'pid':os.getpid(),'birth':birth},'issued_sha256':grant_sha,'lifecycle_sha256_at_use':hashlib.sha256(lifecycle_raw).hexdigest()})
print((output/'proof-result.json').read_text())
