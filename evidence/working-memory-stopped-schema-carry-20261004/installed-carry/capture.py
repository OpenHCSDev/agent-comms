"""Record the one issued carry command; never substitute its implementation."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import zipfile

repo = Path('/home/ts/wt/comms-goal-ledger-schema-carry-20261002')
custody = Path('/home/ts/.cache/agent-scratch/disk-cleanup-owner-20261002')
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
target_grant = custody/'style22-Einstein-Mendel663-Core-only-stopped-carry-issued-grant.json'
source_grant = custody/'thin540-Mendel663-carry-only-source-redeclaration-READ-issued-grant.json'
assert sha(target_grant) == 'ab31680dffc051b2eafb251ae3f5d5cf1183716d53199a1f670a65d00c757b86'
assert sha(source_grant) == 'd7f12b1c1f092c6123abaf2880ef87c37e1f863f705e26c3000c2574ac53415c'
grant = json.loads(target_grant.read_text())
source = json.loads(source_grant.read_text())
life_path = Path(grant['lifecycle'])
life = json.loads(life_path.read_text())
assert life['target_READ_execution_authorized'] and not life['target_READ_execution_held']
assert life['final_execution_binding']['candidate_source_proof_sha256'] == 'ad826643682378d1326c3eeac243440a07424b65fed195c2625180598c84b01d'
proof = Path(life['final_execution_binding']['candidate_source_proof'])
assert sha(proof) == life['final_execution_binding']['candidate_source_proof_sha256']
assert json.loads(Path(source['lifecycle']).read_text())['source_child_inside_bound_target_authorized']
for p, expected in grant['tool_hashes'].items():
    assert sha(Path(p)) == expected
base = Path(grant['base'])
out, err = Path(grant['carry_stdout']), Path(grant['carry_stderr'])
assert not out.exists() and not err.exists() and not (base/'receipt.json').exists()

def write(name, data):
    with (base/name).open('x') as f:
        f.write(json.dumps(data, indent=2)+'\n')

def file_fact(path):
    s = path.lstat()
    return {'mode': s.st_mode, 'inode': s.st_ino, 'mtime_ns': s.st_mtime_ns,
            'link': os.readlink(path) if path.is_symlink() else None,
            'sha256': sha(path) if path.is_file() and not path.is_symlink() else None}

readback = json.loads(Path(source['source_baseline']).read_text())
prefix = Path(source['prefix'])
site = prefix/'lib/python3.14/site-packages'
wheel = Path(readback['current656_wheel'])
assert sha(wheel) == readback['current656_wheel_sha256']
with zipfile.ZipFile(wheel) as z:
    members = [n for n in z.namelist() if n.startswith('agent_comms/')]
    assert len(members) == 347
    for n in members:
        assert (site/n).read_bytes() == z.read(n), n
source_paths = {site/n for n in members}
source_paths.update(Path(item['path']) for item in readback['keepers_manifest'])
source_paths.update(Path(p) for p in readback['current_metadata_hashes'])
before_source = {str(p): file_fact(p) for p in sorted(source_paths)}
target_site = Path(grant['prefix'])/'lib/python3.14/site-packages'
target_paths = set()
for member in json.loads(proof.read_text())['sources']:
    files = {p for p in Path(member['location']).rglob('*') if p.is_file() and '__pycache__' not in p.parts}
    assert len(files) == member['files'], member['module']
    target_paths.update(files)
assert len(target_paths) == 953
target_paths.update(p for p in target_site.glob('*.dist-info/*') if p.is_file())
before_target = {str(p): file_fact(p) for p in sorted(target_paths)}
original = Path(grant['authored_original'])
original_before = {str(p): file_fact(p) for p in original.rglob('*') if p.is_file()}
assert len(original_before) == 15
for item in grant['stopped_original_initial_files']:
    f = file_fact(original/item['relative_path'])
    assert f['sha256'] == item['sha256'] and f['mode'] == item['full_mode']
    assert f['mtime_ns'] == item['mtime_ns'] and f['inode'] == item['original_inode']
write('carry-prefix-before.json', {'source': before_source, 'target': before_target, 'authored_original': original_before})

def process_fact(pid):
    path = Path(f'/proc/{pid}')
    raw = (path/'stat').read_text()
    fields = raw[raw.rindex(')')+2:].split()
    return {'pid': pid, 'birth_ticks': int(fields[19]), 'ppid': int(fields[1]),
            'pgid': int(fields[2]), 'state': fields[0],
            'cmdline': (path/'cmdline').read_bytes().split(b'\0')[:-1]}

env = os.environ.copy()
env.pop('PYTHONPATH', None)
env['PYTHONDONTWRITEBYTECODE'] = '1'
argv = grant['carry_argv']
trace = base/'carry-process.trace'
started = time.monotonic()
seen = {}
with out.open('xb') as stdout, err.open('xb') as stderr:
    child = subprocess.Popen(['/usr/bin/strace', '-ff', '-s', '4096', '-e', 'trace=process', '-o', str(trace), *argv],
                             stdout=stdout, stderr=stderr, cwd=repo, env=env, start_new_session=True)
    while True:
        pending = [child.pid]
        for pid in pending:
            try:
                fact = process_fact(pid)
                fact['cmdline'] = [v.decode(errors='replace') for v in fact['cmdline']]
                if pid not in seen or fact['cmdline']:
                    seen[pid] = fact
                pending.extend(int(n) for n in Path(f'/proc/{pid}/task/{pid}/children').read_text().split())
            except (FileNotFoundError, ProcessLookupError):
                pass
        if child.poll() is not None:
            break
        time.sleep(0.002)
    code = child.wait()
elapsed = time.monotonic()-started
group = child.pid
remaining = []
for p in Path('/proc').iterdir():
    if not p.name.isdigit():
        continue
    try:
        fact = process_fact(int(p.name))
        if fact['pgid'] == group:
            remaining.append(int(p.name))
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        pass
traces = {str(p): sha(p) for p in sorted(base.glob('carry-process.trace.*'))}
for pid, fact in seen.items():
    fact['proc_absent'] = not Path(f'/proc/{pid}').exists()
    path = Path(str(trace)+f'.{pid}')
    fact['process_exit_trace'] = path.read_text() if path.exists() else None
source_after = {p: file_fact(Path(p)) for p in before_source}
target_after = {p: file_fact(Path(p)) for p in before_target}
original_after = {str(p): file_fact(p) for p in original.rglob('*') if p.is_file()}
source_unchanged = source_after == before_source
target_unchanged = target_after == before_target
original_unchanged = original_after == original_before
write('carry-prefix-after.json', {'source': source_after, 'target': target_after, 'authored_original': original_after})
result = {'returncode': code, 'elapsed_seconds': elapsed, 'effective_argv': argv,
          'observation': 'strace process-only observation of unchanged control; proc births sampled for owned descendants',
          'candidate_source_proof_sha256': sha(proof), 'target_grant_sha256': sha(target_grant),
          'source_grant_sha256': sha(source_grant), 'effective_target_lifecycle_sha256': sha(life_path),
          'owned_processes': list(seen.values()), 'joined_group': group, 'remaining_groups': remaining,
          'sockets': [str(p) for p in base.rglob('*') if p.is_socket()],
          'source_members_unchanged': source_unchanged, 'source_members': len(before_source),
          'target_members_unchanged': target_unchanged, 'target_members': len(before_target),
          'authored_original_unchanged': original_unchanged,
          'stdout_sha256': sha(out), 'stderr_sha256': sha(err), 'traces': traces,
          'receipt_present': (base/'receipt.json').exists(), 'App_native_provider_input_seed_copy_calls': 0}
write('carry-terminal.json', result)
print(json.dumps({k:v for k,v in result.items() if k not in ('owned_processes','traces')}, indent=2), flush=True)
assert not remaining and all(f['proc_absent'] for f in seen.values())
assert source_unchanged and target_unchanged and original_unchanged
raise SystemExit(code)
