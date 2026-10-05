import hashlib
import importlib.metadata as metadata
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import zipfile


# Literal issued/read operands are supplied once; destinations derive from issued scope.
issued = Path(sys.argv[1])
issued_sha256 = sys.argv[2]
read_grant = Path(sys.argv[3])
read_sha256 = sys.argv[4]
wheel_proof_sha256 = sys.argv[5]
sha = lambda data: hashlib.sha256(data).hexdigest()
assert sha(issued.read_bytes()) == issued_sha256
assert sha(read_grant.read_bytes()) == read_sha256
grant = json.loads(issued.read_text())
checkout = Path(grant['source_WT'])
output = Path(grant['owned_output'])
proof_path = Path(grant['candidate_sourceproof'])
trust_path = Path(grant['native_FullTrust_receipt'])
inventory_path = output / 'installed-inventory.json'
requirements_path = output / 'installed-requirements.txt'
assert proof_path.parent == trust_path.parent == inventory_path.parent == output
assert output.is_dir()
# Refuse an occupied proof destination before imports, FullTrust, or any write.
for path in (proof_path, trust_path, inventory_path, requirements_path):
    assert not path.exists(), path
lifecycle_bytes = Path(grant['lifecycle']).read_bytes()
lifecycle = json.loads(lifecycle_bytes)
assert lifecycle['package_authorized'] and lifecycle['native_READ_authorized']
assert lifecycle['native_READ_authority']['sha256'] == sha(read_grant.read_bytes())
import agent_comms
from agent_comms.field_codec import FieldCodec
from agent_comms.native_package import MANIFEST, verify_native_package
prefix = Path(sys.prefix)
assert prefix == Path(grant['prefix'])
assert not os.environ.get('PYTHONPATH')
package = Path(agent_comms.__file__).resolve().parent
assert package.is_relative_to(prefix.resolve())
site = package.parent
wheel = Path(grant['candidate_wheel'])
assert sha(wheel.read_bytes()) == lifecycle['candidate_wheel_sha256']
wheel_proof = Path(grant['wheel_proof'])
assert sha(wheel_proof.read_bytes()) == wheel_proof_sha256
original = json.loads(wheel_proof.read_text())
head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=checkout, text=True).strip()
assert head == lifecycle['source_receipt_head']
requests = ''.join(f"{head}:{a['source']}\n" for a in original['assets'])
blobs = io.BytesIO(subprocess.check_output(['git', 'cat-file', '--batch'], input=requests.encode(), cwd=checkout))
inventory = []
with zipfile.ZipFile(wheel) as archive:
    for asset in original['assets']:
        header = blobs.readline().decode().split()
        assert header[1] == 'blob'
        committed = blobs.read(int(header[2]))
        assert blobs.read(1) == b'\n'
        local = (checkout / asset['source']).read_bytes()
        archived = archive.read(asset['member'])
        installed = (site / asset['member']).read_bytes()
        assert committed == local == archived == installed
        assert sha(installed) == asset['sha256']
        inventory.append({**asset, 'installed': str(site / asset['member']), 'Git_local_ZIP_installed_equal': True})
    for member, expected in original['metadata'].items():
        assert sha(archive.read(member)) == expected
        if not member.endswith('/RECORD'):
            assert (site / member).read_bytes() == archive.read(member)
actual = {p.relative_to(site).as_posix() for p in package.rglob('*') if p.is_file() and '__pycache__' not in p.parts}
expected = {a['member'] for a in original['assets']}
assert actual == expected and len(actual) == 355
floor_path = Path(grant['actual_floor_authority']['path'])
assert sha(floor_path.read_bytes()) == grant['actual_floor_authority']['sha256']
floor = json.loads(floor_path.read_text())
unchanged = []
for record in floor['original_records']:
    path = Path(record['path'])
    relative = path.relative_to(prefix).as_posix()
    if '/agent_comms/' in relative or '/agent_comms-0.1.0.dist-info/' in relative:
        continue
    info = path.lstat()
    assert stat.S_IMODE(info.st_mode) == record['mode'], path
    if record['kind'] == 'file':
        assert path.is_file() and sha(path.read_bytes()) == record['sha256'], path
    elif record['kind'] in ('symlink', 'link'):
        assert path.is_symlink() and os.readlink(path) == record.get('target', record.get('link')), path
    else:
        raise AssertionError(record)
    unchanged.append(record)
distributions = {d.metadata['Name']: d for d in metadata.distributions()}
normalize = lambda name: name.lower().replace('_', '-').replace('.', '-')
versions = {normalize(name): d.version for name, d in distributions.items()}
assert versions == {normalize(k): v for k, v in floor['normal_distributions'].items()}
core = metadata.distribution('agent-comms')
direct = json.loads(core.read_text('direct_url.json'))
assert direct['url'] == wheel.as_uri()
origins = {name: json.loads(d.read_text('direct_url.json')) if d.read_text('direct_url.json') else None for name, d in distributions.items()}
metadata_inventory = []
for distribution in distributions.values():
    for path in sorted(Path(distribution._path).rglob('*')):
        if path.is_file():
            metadata_inventory.append({'path': str(path), 'sha256': sha(path.read_bytes()), 'mode': stat.S_IMODE(path.stat().st_mode)})
assert len(metadata_inventory) == 69
requirements = ''.join(f'{name}=={version}\n' for name, version in sorted(versions.items()))
with requirements_path.open('x') as stream:
    stream.write(requirements)
birth = int(Path(f'/proc/{os.getpid()}/stat').read_text().rsplit(')', 1)[1].split()[19])
native = grant['matching_native_reference']
assert sha(MANIFEST.read_bytes()) == native['manifest']
begin = time.monotonic()
verify_native_package(Path(native['package']))
trust = {'state': 'PASS', 'controller': {'pid': os.getpid(), 'start_time': birth}, 'elapsed_seconds': time.monotonic() - begin,
         'native_package': native['package'], 'native_manifest': native['manifest'], 'native_tree': native['tree'],
         'owner': 'agent_comms.native_package.verify_native_package', 'READ_grant': str(read_grant),
         'READ_grant_sha256': sha(read_grant.read_bytes()), 'native_processes_started': 0, 'inputs': 0}
with trust_path.open('x') as stream:
    stream.write(json.dumps(trust, indent=2) + '\n')
with inventory_path.open('x') as stream:
    stream.write(json.dumps({'assets': inventory, 'extra_assets': [], 'unchanged_original_records': unchanged,
                                     'metadata': metadata_inventory, 'versions': versions, 'origins': origins}, indent=2) + '\n')
sys.path.insert(0, str(checkout / 'tools/cutover'))
from publish_retained_summary import InstalledSource, InstalledSourceProof, ArchivePackageDirectUrl, PackageArchiveInfo, ReviewedArtifact
artifact = ReviewedArtifact(wheel, lifecycle['candidate_wheel_sha256'])
source = InstalledSource('agent_comms', head, str(package), 355, sum(a['member'].endswith('.py') for a in inventory), True,
                         ArchivePackageDirectUrl(direct['url'], PackageArchiveInfo(**direct['archive_info'])), sha(inventory_path.read_bytes()))
source.require_package('agent_comms', package, direct, (artifact,))
proof = InstalledSourceProof('PASS installed byte/origin and native READ trust; canonical/native execution not started', prefix, (source,),
                            Path(native['package']), str(Path(native['package']) / 'dist/cli.js'), native['manifest'], native['tree'], True,
                            metadata.version('agent-client-protocol'), len(distributions), tuple(sorted(versions.items())), sha(requirements.encode()),
                            len(unchanged), True, False, False, False, False, (),
                            grant['proof_strength'], (artifact,))
result = FieldCodec.encode(proof)
result['qualification_evidence'] = {'issued_grant': str(issued), 'issued_sha256': sha(issued.read_bytes()),
                                  'inventory': str(inventory_path), 'inventory_sha256': sha(inventory_path.read_bytes()),
                                  'native_full_trust_receipt': str(trust_path), 'native_full_trust_receipt_sha256': sha(trust_path.read_bytes()),
                                  'source_head': head, 'wheel_build_source': original['source_head'],
                                  'installed_assets': 355, 'original_nonCore_keepers': 94, 'metadata_files': 69,
                                  'other9_origins_unchanged': True, 'controller': trust['controller'],
                                  'native_execution_started': False, 'lifecycle_sha256_at_proof': sha(lifecycle_bytes), 'control_sha256': grant['control_sha256']}
with proof_path.open('x') as stream:
    stream.write(json.dumps(result, indent=2) + '\n')
print(json.dumps({'proof': str(proof_path), 'sha256': sha(proof_path.read_bytes()), 'FullTrust': str(trust_path),
                  'FullTrust_sha256': sha(trust_path.read_bytes()), 'assets': 355, 'metadata': 69, 'distributions': len(distributions)}))
