from pathlib import Path
import hashlib, json, os, shutil, subprocess, time

OUT = Path(__file__).parent
subprocess.run(['sudo', '-n', 'python', '-B', str(OUT / 'census.py')], check=True, stdout=subprocess.DEVNULL)
census = json.loads((OUT / 'reference-census.json').read_text())
assert not census['processes']['references']
assert not census['processes']['inaccessible_same_user_processes']
assert not census['configuration']['references']
assert not census['configuration']['errors']
protected = ['runtime-sidebar-native-custody-20260930',
    'runtime-viewport-native-residency-20260930', 'runtime-native-custody-integrated-20260930',
    'runtime-all-merged-20260930', 'runtime-native-budget-request-progress-20260930']
parent = Path('/home/ts/.local/share/agent-comms')
free_before = shutil.disk_usage(parent).free
receipt = dict(owner='Schrodinger', timestamp=time.time(), removed=[],
    protected_prefixes=protected, native_packages_preserved=True,
    source_worktrees_preserved=True, original_proofs_and_unknown_inputs_preserved=True,
    reference_census_sha256=hashlib.sha256((OUT / 'reference-census.json').read_bytes()).hexdigest())
for path in census['candidates']:
    p = Path(path)
    assert p.parent == parent and p.name not in protected and not p.is_symlink()
    staging = json.loads((p / 'staging-receipt.json').read_text())
    assert staging['owner'] in {'Schrodinger243stage'}
    archive = OUT / 'retired-runtime-provenance' / p.name
    archive.mkdir(parents=True, exist_ok=True)
    for f in p.iterdir():
        if f.is_file() and not f.is_symlink(): shutil.copy2(f, archive / f.name)
    size = exclusive = dirs_size = files = 0
    for current, dirs, names in os.walk(p):
        dirs_size += Path(current).stat().st_blocks * 512
        for name in names:
            f = Path(current) / name
            assert not (name.endswith('.jsonl') or name in {'registry.json', 'bus.jsonl', 'session.json'})
            stat = f.lstat()
            size += stat.st_blocks * 512
            if stat.st_nlink == 1: exclusive += stat.st_blocks * 512
            files += 1
    item = dict(path=path, allocated_file_bytes=size,
        reclaimable_bytes_at_removal=exclusive + dirs_size, files=files,
        provenance_archive=str(archive), staging_owner=staging['owner'])
    shutil.rmtree(p)
    assert not p.exists()
    receipt['removed'].append(item)
    (OUT / 'cleanup-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
receipt['free_before_bytes'] = free_before
receipt['free_after_bytes'] = shutil.disk_usage(parent).free
receipt['observed_net_free_increase_bytes'] = receipt['free_after_bytes'] - free_before
receipt['reclaimed_allocated_bytes'] = sum(x['reclaimable_bytes_at_removal'] for x in receipt['removed'])
receipt['measurement_limit'] = 'Observed filesystem net includes concurrent writes; reclaimable allocation excludes surviving hardlinks.'
receipt['state'] = 'completed'
(OUT / 'cleanup-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
print(json.dumps(receipt, indent=2))
