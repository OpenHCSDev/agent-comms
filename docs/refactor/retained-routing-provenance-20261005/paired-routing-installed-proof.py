"""Prospective paired routing source proof; stdlib reads, no application/native imports."""
import base64
import configparser
import csv
import hashlib
import importlib.metadata as metadata
import io
import json
import os
from pathlib import Path
import stat
import sys
import zipfile


def digest(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def read(path, expected):
    assert digest(path) == expected, path
    return json.loads(Path(path).read_text())


def original(record):
    path = Path(record['path'])
    assert stat.S_IMODE(path.lstat().st_mode) == record['mode'], path
    if record['kind'] == 'symlink':
        assert os.readlink(path) == record['target'], path
    else:
        assert digest(path) == record['sha256'], path


def package(wheel, site, module, assets):
    path = Path(wheel['path'])
    assert digest(path) == wheel['sha256']
    with zipfile.ZipFile(path) as archive:
        files = {item.filename for item in archive.infolist() if not item.is_dir()}
        assert len(files) == sum(not item.is_dir() for item in archive.infolist())
        record = next(name for name in files if name.endswith('.dist-info/RECORD'))
        entries = list(csv.reader(io.StringIO(archive.read(record).decode())))
        assert len(entries) == len({entry[0] for entry in entries})
        assert {entry[0] for entry in entries} == files
        members = {name for name in files if name.startswith(module + '/')}
        assert len(members) == assets
        actual = {str(p.relative_to(site)) for p in (site / module).rglob('*')
                  if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}
        assert actual == members, actual.symmetric_difference(members)
        for name, checksum, size in entries:
            data = archive.read(name)
            if name == record:
                assert checksum == size == ''
            else:
                algorithm, encoded = checksum.split('=', 1)
                assert base64.urlsafe_b64encode(hashlib.new(algorithm, data).digest()).decode().rstrip('=') == encoded
                assert len(data) == int(size)
                assert (site / name).read_bytes() == data, name
            if name in members:
                assert stat.S_IMODE((site / name).stat().st_mode) == stat.S_IMODE(archive.getinfo(name).external_attr >> 16)
        points = configparser.ConfigParser()
        entry_points = record.removesuffix('RECORD') + 'entry_points.txt'
        if entry_points not in files:
            return members, set()
        points.read_string(archive.read(entry_points).decode())
        return members, set(points['console_scripts']) if points.has_section('console_scripts') else set()


proposal_path, proposal_sha, target_grant, target_sha, source_grant, source_sha = sys.argv[1:]
p = read(proposal_path, proposal_sha)
tg, sg = read(target_grant, target_sha), read(source_grant, source_sha)
assert tg['prefix'] == p['target']['prefix'] and sg['prefix'] == p['source']['prefix']
target_lifecycle = json.loads(Path(tg['lifecycle']).read_text())
source_lifecycle = json.loads(Path(sg['lifecycle']).read_text())
assert source_lifecycle['proof_execution_authorized'] and not source_lifecycle['execution_authorized']
assert target_lifecycle['proof_execution_authorized'] and not target_lifecycle['execution_authorized']
assert not target_lifecycle['native_EXEC_authorized'] and not source_lifecycle['native_EXEC_authorized']
assert not os.environ.get('PYTHONPATH') and not os.environ.get('PYTHONHOME')
for row in p['tools']:
    assert digest(row['path']) == row['sha256']
source_floor = read(p['source']['floor_preimage']['path'], p['source']['floor_preimage']['sha256'])[p['source']['floor_preimage']['section']]
target_floor = read(p['target']['floor_preimage']['path'], p['target']['floor_preimage']['sha256'])[p['target']['floor_preimage']['section']]
source_site = Path(p['source']['site_packages'])
target_site = Path(p['target']['site_packages'])
source_members, old_bins = package(p['source']['wheel'], source_site, 'agent_comms', 297)
target_members = set()
current_bins = set()
for declared in p['target']['proof_wheels']:
    members, entrypoints = package(declared['wheel'], target_site, declared['module'], declared['assets'])
    target_members.update(members)
    current_bins.update(entrypoints)
assert len(target_members) == p['target']['total_assets']
source_prefix = Path(p['source']['prefix'])
for row in source_floor['original_records']:
    path = Path(row['path'])
    changed = path.is_relative_to(source_site / 'agent_comms') or path.is_relative_to(source_site / 'agent_comms-0.1.0.dist-info')
    if not changed and path not in {source_prefix / 'bin' / name for name in old_bins | set(p['source']['restored_Core_console_scripts'])}:
        original(row)
for name, row in source_floor['original_keepers'].items():
    path = Path(source_floor['original_keeper_base']) / name
    assert digest(path) == row['sha256'] and path.stat().st_size == row['bytes'], path
for row in target_floor['original_records']:
    path = Path(row['path'])
    changed = any(path.is_relative_to(target_site / module) or path.is_relative_to(target_site / distinfo)
                  for module, distinfo in p['target']['replaced_packages'])
    if not changed and path not in {Path(p['target']['prefix']) / 'bin' / name for name in current_bins | set(p['target']['original_replaced_console_scripts'])}:
        original(row)
for name, row in target_floor['protected_originals'].items():
    original({'path': name, 'kind': 'file', **row})
for row in target_floor['unchanged_other66_unique_records'] + target_floor['additional_original_environment_nodes']:
    original(row)
versions = {}
for label, site, expected in [('source', source_site, p['source']['normal_distributions']), ('target', target_site, p['target']['normal_distributions'])]:
    distributions = {d.metadata['Name'].lower().replace('_', '-'): d for d in metadata.distributions(path=[str(site)])}
    actual = {name: dist.version for name, dist in distributions.items()}
    assert actual == {name.lower().replace('_', '-'): version for name, version in expected.items()}
    versions[label] = actual
# Source filewheel origin is acquired from its own metadata, never the target's.
source_dist = next(d for d in metadata.distributions(path=[str(source_site)]) if d.metadata['Name'] == 'agent-comms')
source_origin = json.loads(source_dist.read_text('direct_url.json'))
assert source_origin['url'] == Path(p['source']['wheel']['path']).as_uri() and 'archive_info' in source_origin
source_manifest = source_site / 'agent_comms/_native/pi-native.sha256'
target_manifest = target_site / 'agent_comms/_native/pi-native.sha256'
assert digest(source_manifest) == p['source']['native_resource_manifest_sha256']
assert digest(target_manifest) == p['native_READ']['manifest']
target_origins = {}
for declaration in p['target']['staged_packages']:
    dist = next(d for d in metadata.distributions(path=[str(target_site)]) if d.metadata['Name'] == declaration['distribution'])
    origin = json.loads(dist.read_text('direct_url.json'))
    assert origin['url'] == Path(declaration['wheel']['path']).as_uri() and 'archive_info' in origin
    target_origins[declaration['distribution']] = origin
for label, site in [('source', source_site), ('target', target_site)]:
    for distribution, expected in p[label]['unchanged_direct_urls'].items():
        actual = next(d for d in metadata.distributions(path=[str(site)]) if d.metadata['Name'] == distribution).read_text('direct_url.json')
        if expected is None:
            assert actual is None, distribution
        else:
            assert actual is not None and hashlib.sha256(actual.encode()).hexdigest() == expected['sha256'], distribution
result = {'scope': 'Exact newly-built old720297 and current355+319 installed files; no application/native import',
          'source_commit': p['source']['commit'], 'source_assets': 297,
          'target_Core_commit': p['target']['Core_commit'], 'target_Toad_commit': p['target']['Toad_commit'],
          'target_total_assets': len(target_members), 'versions': versions,
          'source_direct_url': source_origin, 'target_filewheel_origins': target_origins,
          'source_nonCore_floor_unchanged': True, 'target_nonreplaced_floor_unchanged': True,
          'source_old_native_full_trust': False, 'source_old_native_resource_only': True,
          'target_native_full_trust': False, 'native_READ_or_execution_performed': False,
          'target_original_full_trust_required_before_SDK': p['native_READ'],
          'proposal_sha256': proposal_sha, 'source_grant_sha256': source_sha, 'target_grant_sha256': target_sha}
output = Path(p['owned_output'])
assert output == Path(tg['owned_output']) and output == Path(sg['owned_output'])
with (output / 'candidate-source-proof.json').open('x') as destination:
    destination.write(json.dumps(result, indent=2) + '\n')
print(json.dumps({'state': 'PAIRED SOURCE PROOF PASS; CONTROL HELD', 'proof': str(output / 'candidate-source-proof.json'),
                  'sha256': digest(output / 'candidate-source-proof.json')}), flush=True)
