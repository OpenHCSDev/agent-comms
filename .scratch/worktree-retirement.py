"""Inventory persistent checkouts; remove only published, finished, unused trees.

Owner doctrine-segment; requested 2026-10-15. No reset, clean, forced removal,
branch deletion, package edits or session cleanup. Evidence stays in this tree.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys

BASE = Path('/home/ts/wt')
OUT = Path('/home/ts/.cache/agent-scratch/nominal-pi-main')
parser = argparse.ArgumentParser()
parser.add_argument('--remove', action='store_true')
args = parser.parse_args()

def run(*command, cwd=None, timeout=30):
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, timeout=timeout)

def git(path, *arguments):
    result = run('git', '-C', str(path), *arguments)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()

paths = sorted(p for p in BASE.iterdir() if p.is_dir())
root_strings = list(map(str, paths))
who = run('agent-comms', 'who')
assert who.returncode == 0, who.stderr
people = json.loads(who.stdout)['who']
(OUT/'cleanup-active-units.json').write_text(who.stdout)

def process_scan():
    result = subprocess.run(['sudo','-n','/usr/bin/python3','-I','-S',str(Path(__file__).with_name('worktree-process-references.py'))], input=json.dumps(root_strings), capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)

processes = process_scan()
(OUT/'cleanup-process-references.json').write_text(json.dumps(processes, indent=2))

# Installed editable packages/plain .pth paths are real borrowers even when idle.
borrowers = {p: [] for p in root_strings}
for scan_root in (BASE, Path('/home/ts/.local/share/agent-comms/runtimes')):
    if not scan_root.is_dir():
        continue
    for directory, dirs, files in os.walk(scan_root, followlinks=False):
        dirs[:] = [name for name in dirs if name not in {'.git','node_modules','__pycache__','.pytest_cache'}]
        current = Path(directory)
        if current.name == 'site-packages':
            for file in current.glob('*.pth'):
                try:
                    text = file.read_text(errors='replace')
                except OSError:
                    continue
                for target in root_strings:
                    if target in text and not file.is_relative_to(target):
                        borrowers[target].append(str(file))
        if current.name.endswith('.dist-info') and 'direct_url.json' in files:
            try:
                data = json.loads((current/'direct_url.json').read_text())
            except (OSError, ValueError):
                continue
            if data.get('dir_info', {}).get('editable'):
                for target in root_strings:
                    if data.get('url', '').startswith('file://'+target+'/') or data.get('url') == 'file://'+target:
                        if not current.is_relative_to(target):
                            borrowers[target].append(str(current/'direct_url.json'))

repos = {}
rows = []
for path in paths:
    row = {'path': str(path), 'owner': [], 'branch': None, 'pushed': False, 'merged_or_closed': False, 'dirty': True, 'process_reference': bool(processes['references'][str(path)]), 'processes': processes['references'][str(path)], 'borrowers': borrowers[str(path)], 'reasons': [], 'removed': False}
    related = [p for p in people if p.get('worktree') == str(path)]
    row['owner'] = [p['name'] for p in sorted(related,key=lambda p:p.get('created_at',0),reverse=True)[:3]]
    active = [p['name'] for p in related if p.get('status') != 'stopped' or p.get('goal') is not None or p.get('active_turn') is not None]
    if active:
        row['reasons'].append('active unit: '+', '.join(active))
    if path.name in {'toad-live','core-live'}:
        row['reasons'].append('explicitly protected')
    if path.is_symlink():
        row['reasons'].append('symlink, not an owned checkout directory')
    if path.stat().st_uid != os.getuid():
        row['reasons'].append('different filesystem owner')
    try:
        common = git(path,'rev-parse','--path-format=absolute','--git-common-dir')
        row['common_git_dir'] = common
        row['head'] = git(path,'rev-parse','HEAD')
        row['branch'] = git(path,'rev-parse','--abbrev-ref','HEAD')
        row['status'] = git(path,'status','--porcelain','--untracked-files=normal')
        row['dirty'] = bool(row['status'])
        if Path(common) == path/'.git':
            row['reasons'].append('owning repository/object store')
        records = git(path,'worktree','list','--porcelain').split('\n\n')
        record = next((r for r in records if r.splitlines()[0] == 'worktree '+str(path)), '')
        if any(line.startswith('locked') for line in record.splitlines()):
            row['reasons'].append('git worktree locked')
        if common not in repos:
            url = git(path,'remote','get-url','origin')
            match = re.search(r'github\.com[:/](.+?)(?:\.git)?$',url)
            remote = run('git','ls-remote','--heads',url,timeout=45)
            refs = {line.split()[1]:line.split()[0] for line in remote.stdout.splitlines()} if remote.returncode == 0 else {}
            repo = {'url':url,'refs':refs,'prs':[],'default':'main','errors':[]}
            if match:
                name = match.group(1)
                info = run('gh','repo','view',name,'--json','defaultBranchRef',timeout=30)
                if info.returncode == 0:
                    repo['default'] = json.loads(info.stdout)['defaultBranchRef']['name']
                prs = run('gh','pr','list','--repo',name,'--state','all','--limit','1000','--json','number,headRefOid,headRefName,state',timeout=60)
                if prs.returncode == 0:
                    repo['prs'] = json.loads(prs.stdout)
                else:
                    repo['errors'].append(prs.stderr.strip())
            if remote.returncode:
                repo['errors'].append(remote.stderr.strip())
            repos[common] = repo
        repo = repos[common]
        matching = [p for p in repo['prs'] if p['headRefOid'] == row['head']]
        row['prs'] = matching
        published = row['head'] in repo['refs'].values() or bool(matching)
        default_tip = repo['refs'].get('refs/heads/'+repo['default'])
        merged = False
        if default_tip:
            result = run('git','-C',str(path),'merge-base','--is-ancestor',row['head'],default_tip)
            merged = result.returncode == 0
        row['pushed'] = published or merged
        row['merged_or_closed'] = merged or any(p['state'] in {'MERGED','CLOSED'} for p in matching)
        if repo['errors']:
            row['publication_errors'] = repo['errors']
        # Do not erase saved scientific/session/auth/wire data hidden by ignore
        # rules. Dependencies and ordinary compiled caches are not originals.
        sensitive = []
        for directory, dirs, files in os.walk(path,followlinks=False):
            dirs[:] = [name for name in dirs if name not in {'.git','.venv','node_modules','site-packages','__pycache__','.pytest_cache'}]
            for name in files:
                file = Path(directory)/name
                if name in {'auth.json','registry.json','input_dispositions.sqlite3'} or file.suffix.lower() in {'.jsonl','.nd2','.czi','.lif','.tif','.tiff','.hdf5','.h5'}:
                    relative = str(file.relative_to(path))
                    if run('git','-C',str(path),'check-ignore','--quiet','--',relative).returncode == 0:
                        sensitive.append(relative)
        row['ignored_originals'] = sensitive
        if sensitive:
            row['reasons'].append('ignored saved/session/scientific data: '+', '.join(sensitive[:4]))
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        row['reasons'].append('inventory error: '+str(error))
    if row['dirty']:
        row['reasons'].append('uncommitted/untracked files')
    if not row['pushed']:
        row['reasons'].append('HEAD publication unproved')
    if not row['merged_or_closed']:
        row['reasons'].append('branch not proved merged/closed')
    if row['process_reference']:
        row['reasons'].append('referenced by process')
    if row['borrowers']:
        row['reasons'].append('installed package borrows path')
    if processes['errors']:
        row['reasons'].append('incomplete /proc scan')
    row['eligible'] = not row['reasons']
    rows.append(row)

before = os.statvfs(BASE)
receipts = []
if args.remove:
    # Refresh live unit and every /proc field immediately before deletion.
    latest = json.loads(run('agent-comms','who').stdout)['who']
    live = process_scan()
    assert not live['errors'], live['errors']
    for row in rows:
        if not row['eligible']:
            continue
        path = Path(row['path'])
        if any(p.get('worktree') == str(path) and (p.get('status') != 'stopped' or p.get('goal') is not None or p.get('active_turn') is not None) for p in latest) or live['references'][str(path)]:
            row['reasons'].append('new active owner/process at deletion checkpoint')
            continue
        if git(path,'rev-parse','HEAD') != row['head'] or git(path,'status','--porcelain','--untracked-files=normal'):
            row['reasons'].append('source changed at deletion checkpoint')
            continue
        size = run('du','-sx','--block-size=1',str(path))
        row['directory_bytes'] = int(size.stdout.split()[0]) if size.returncode == 0 else None
        result = run('git','--git-dir',row['common_git_dir'],'worktree','remove',str(path),timeout=90)
        row['removed'] = result.returncode == 0 and not path.exists()
        receipts.append({'path':str(path),'returncode':result.returncode,'stderr':result.stderr})
        if not row['removed']:
            row['reasons'].append('git worktree remove refused: '+result.stderr.strip())
after = os.statvfs(BASE)
report = {'checked_at':datetime.datetime.now().astimezone().isoformat(),'rows':rows,'process_errors':processes['errors'],'repositories':repos,'removal_receipts':receipts,'observed_free_bytes_increase':(after.f_bavail-before.f_bavail)*after.f_frsize}
name = 'worktrees-removed' if args.remove else 'worktrees-inventory'
(OUT/(name+'.json')).write_text(json.dumps(report,indent=2))
with (OUT/(name+'.tsv')).open('w') as output:
    print('path\tbranch\tpushed\tmerged_or_closed\tuncommitted\tprocess_reference\towner\tresult/reason',file=output)
    for row in rows:
        print(row['path'],row['branch'],row['pushed'],row['merged_or_closed'],row['dirty'],row['process_reference'],','.join(row['owner']) or 'unknown', 'REMOVED' if row['removed'] else '; '.join(row['reasons']) or 'ELIGIBLE',sep='\t',file=output)
print(json.dumps({'rows':len(rows),'eligible':[r['path'] for r in rows if r['eligible']],'removed':[r['path'] for r in rows if r['removed']],'process_errors':processes['errors'],'observed_free_bytes_increase':report['observed_free_bytes_increase']}))
