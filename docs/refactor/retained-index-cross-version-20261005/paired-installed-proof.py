"""Issued paired index source proof; stdlib reads, no application/native imports."""
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


def package(wheel, site, assets):
    path = Path(wheel['path'])
    assert digest(path) == wheel['sha256']
    with zipfile.ZipFile(path) as archive:
        files = {item.filename for item in archive.infolist() if not item.is_dir()}
        assert len(files) == sum(not item.is_dir() for item in archive.infolist())
        record = next(name for name in files if name.endswith('.dist-info/RECORD'))
        entries = list(csv.reader(io.StringIO(archive.read(record).decode())))
        assert len(entries) == len({entry[0] for entry in entries})
        assert {entry[0] for entry in entries} == files
        members = {name for name in files if name.startswith('agent_comms/')}
        assert len(members) == assets
        actual = {str(p.relative_to(site)) for p in (site / 'agent_comms').rglob('*')
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
        points.read_string(archive.read(record.removesuffix('RECORD') + 'entry_points.txt').decode())
        return members, set(points['console_scripts'])


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
source_floor = read(p['source']['floor_preimage']['path'], p['source']['floor_preimage']['sha256'])
target_floor = read(p['target']['floor_preimage']['path'], p['target']['floor_preimage']['sha256'])
source_site = Path(p['source']['site_packages'])
target_site = Path(p['target']['site_packages'])
source_members, old_bins = package(p['source']['wheel'], source_site, 297)
target_members, current_bins = package(p['target']['wheel'], target_site, 347)
source_prefix = Path(p['source']['prefix'])
for row in source_floor['original_records']:
    path = Path(row['path'])
    changed = path.is_relative_to(source_site / 'agent_comms') or path.is_relative_to(source_site / 'agent_comms-0.1.0.dist-info')
    if not changed and path not in {source_prefix / 'bin' / name for name in old_bins | current_bins}:
        original(row)
for row in target_floor['original_old_preimage_records_current_bytes']:
    original(row)
for name, row in target_floor['protected_originals'].items():
    original({'path': name, 'kind': 'file', **row})
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
result = {'scope': '297 newly-built exact-old-source assets plus reused target347; no application/native import or execution',
          'source_commit': p['source']['commit'], 'source_prefix': p['source']['prefix'], 'source_assets': 297,
          'target_prefix': p['target']['prefix'], 'target_commit': p['target']['commit'], 'target_assets': 347,
          'target_source_relation': 'Original qualified c656/5f plus eight byte-matched consumed files; no latest MAIN equality',
          'versions': versions, 'source_direct_url': source_origin, 'target_original_floor_unchanged': True,
          'source_nonCore_floor_unchanged': True, 'source_old_native_full_trust': False,
          'source_old_native_resource_only': True, 'target_native_full_trust': False,
          'target_native_READ_required_at_control': p['native_READ'], 'native_EXEC': False,
          'proposal_sha256': proposal_sha, 'source_grant_sha256': source_sha, 'target_grant_sha256': target_sha}
output = Path(p['owned_output'])
assert output == Path(tg['owned_output']) and output == Path(sg['owned_output'])
with (output / 'candidate-source-proof.json').open('x') as destination:
    destination.write(json.dumps(result, indent=2) + '\n')
print(json.dumps({'state': 'PAIRED SOURCE PROOF PASS; CONTROL HELD', 'proof': str(output / 'candidate-source-proof.json'),
                  'sha256': digest(output / 'candidate-source-proof.json')}), flush=True)
