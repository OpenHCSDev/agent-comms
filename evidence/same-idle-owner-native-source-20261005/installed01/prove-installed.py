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


checkout = Path('/home/ts/wt/comms-goal-ledger-schema-carry-20261002')
output = checkout / '.artifacts/c3-original-owner-boundaries675-installed02'
issued = Path('/home/ts/.cache/agent-scratch/disk-cleanup-owner-20261002/thin540-Mendel676-corrected-seven-saved-fork-build-package-native-issued-grant.json')
read_grant = Path('/home/ts/wt/toad-prompt-action-owner-20261002/.artifacts/native-keeper676-corrected-seven-READ-issued-20261005.json')
sha = lambda data: hashlib.sha256(data).hexdigest()
assert sha(issued.read_bytes()) == '412e9ce310f3dd80cc1ecec04e5cbde7ec8c1399b0fd8f7807a889b18733ec1c'
assert sha(read_grant.read_bytes()) == '3363d7d90558340beea5b022aa774d401198ddd83bdb982235402b045e38ed1c'
grant = json.loads(issued.read_text())
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
assert sha(wheel_proof.read_bytes()) == 'd38ca0aa5da8df50afc95f1f85473d01dfa80831f71a804fd51905c7416293b2'
original = json.loads(wheel_proof.read_text())
head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=checkout, text=True).strip()
assert head == grant['source_receipt_head']
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
(output / 'installed-requirements.txt').write_text(requirements)
birth = int(Path(f'/proc/{os.getpid()}/stat').read_text().rsplit(')', 1)[1].split()[19])
native = grant['matching_native_reference']
assert sha(MANIFEST.read_bytes()) == native['manifest']
begin = time.monotonic()
verify_native_package(Path(native['package']))
trust = {'state': 'PASS', 'controller': {'pid': os.getpid(), 'start_time': birth}, 'elapsed_seconds': time.monotonic() - begin,
         'native_package': native['package'], 'native_manifest': native['manifest'], 'native_tree': native['tree'],
         'owner': 'agent_comms.native_package.verify_native_package', 'READ_grant': str(read_grant),
         'READ_grant_sha256': sha(read_grant.read_bytes()), 'native_processes_started': 0, 'inputs': 0}
trust_path = output / 'native-full-trust.json'
trust_path.write_text(json.dumps(trust, indent=2) + '\n')
inventory_path = output / 'installed-inventory.json'
inventory_path.write_text(json.dumps({'assets': inventory, 'extra_assets': [], 'unchanged_original_records': unchanged,
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
                            'Seven original serial saved-SDK same-owner source/custody cases remain unrun; one intended localhost input, six no-input variants, source/cancel/successor refusal and restored lock/context observations. No public/UI/performance claim.', (artifact,))
result = FieldCodec.encode(proof)
result['qualification_evidence'] = {'issued_grant': str(issued), 'issued_sha256': sha(issued.read_bytes()),
                                  'inventory': str(inventory_path), 'inventory_sha256': sha(inventory_path.read_bytes()),
                                  'native_full_trust_receipt': str(trust_path), 'native_full_trust_receipt_sha256': sha(trust_path.read_bytes()),
                                  'source_head': head, 'wheel_build_source': original['source_head'],
                                  'installed_assets': 355, 'original_nonCore_keepers': 94, 'metadata_files': 69,
                                  'other9_origins_unchanged': True, 'controller': trust['controller'],
                                  'native_execution_started': False, 'lifecycle_sha256_at_proof': sha(lifecycle_bytes), 'control_sha256': grant['control_sha256']}
proof_path = output / 'candidate-source-proof.json'
assert not proof_path.exists()
proof_path.write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps({'proof': str(proof_path), 'sha256': sha(proof_path.read_bytes()), 'FullTrust': str(trust_path),
                  'FullTrust_sha256': sha(trust_path.read_bytes()), 'assets': 355, 'metadata': 69, 'distributions': len(distributions)}))
