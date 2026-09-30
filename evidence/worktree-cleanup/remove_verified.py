import json, pathlib, subprocess, os, time
out=pathlib.Path(__file__).parent
rows=json.loads((out/'audit.json').read_text())['rows']; records=[]
before=os.statvfs('/home/ts');start_free=before.f_bavail*before.f_frsize
protected=set()
for p in [pathlib.Path('/home/ts/.agent-comms/registry.json'),pathlib.Path('/var/tmp/agent-comms-live-20260927-wzjtqhza/registry.json')]:
 try:
  for r in json.loads(p.read_text()).get('threads',{}).values():
   if r.get('worktree'): protected.add(os.path.realpath(r['worktree']))
 except (OSError,ValueError): pass
# Preserve editable installations and repository anchors.
for base in ['/home/ts/.local/share/agent-comms','/home/ts/.agent-comms/stack']:
 for p in pathlib.Path(base).rglob('*.pth'):
  try:
   for line in p.read_text().splitlines():
    if line.startswith('/home/ts/wt/'):
     parts=pathlib.Path(line).parts;protected.add(str(pathlib.Path(*parts[:5])))
  except (OSError,UnicodeError): pass

def process_ref(path):
 for p in pathlib.Path('/proc').iterdir():
  if not p.name.isdigit(): continue
  try:
   if p.stat().st_uid!=os.getuid(): continue
   cwd=os.readlink(p/'cwd');raw=(p/'cmdline').read_bytes()+(p/'environ').read_bytes()
   if cwd==path or cwd.startswith(path+'/') or path.encode()+b'/' in raw: return True
  except OSError: continue
 return False
for r in rows:
 if r['decision']!='removable': continue
 path=r['path'];p=pathlib.Path(path)
 record={k:r[k] for k in ['path','branch','head','bytes']}
 if not p.exists(): record['result']='already-removed'
 elif path in protected or r['common'].startswith(path+'/'): record['result']='protected-anchor-or-install'
 elif process_ref(path): record['result']='new-process-reference'
 else:
  status=subprocess.run(['git','status','--porcelain=v1','--untracked-files=all'],cwd=path,text=True,capture_output=True)
  head=subprocess.run(['git','rev-parse','HEAD'],cwd=path,text=True,capture_output=True)
  if status.returncode or status.stdout.strip() or head.stdout.strip()!=r['head']: record['result']='changed-since-audit'
  else:
   removed=subprocess.run(['git','--git-dir',r['common'],'worktree','remove',path],capture_output=True,text=True)
   record['result']='removed' if removed.returncode==0 else 'git-refused'
   if removed.returncode: record['reason']=removed.stderr.strip()[:400]
 records.append(record)
 (out/'removal-receipt.json').write_text(json.dumps({'started_free_bytes':start_free,'records':records},indent=2)+'\n')
after=os.statvfs('/home/ts');end_free=after.f_bavail*after.f_frsize
receipt={'started_free_bytes':start_free,'finished_free_bytes':end_free,'free_bytes_change':end_free-start_free,'removed_bytes_estimate':sum(r['bytes'] for r in records if r['result']=='removed'),'records':records}
(out/'removal-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
from collections import Counter
print(json.dumps({k:v for k,v in receipt.items() if k!='records'},indent=2));print(dict(Counter(r['result'] for r in records)))
print('refusals',[(r['path'],r.get('reason')) for r in records if r['result']=='git-refused'])
