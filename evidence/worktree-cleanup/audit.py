import concurrent.futures, json, os, pathlib, subprocess, time
base=pathlib.Path('/home/ts/wt'); out=pathlib.Path(__file__).parent

def cmd(args,cwd=None):
 p=subprocess.run(args,cwd=cwd,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=45)
 return p.returncode,p.stdout.strip()

protected=set()
# Registry references are retained even for stopped agents.
for p in [pathlib.Path('/home/ts/.agent-comms/registry.json'),pathlib.Path('/var/tmp/agent-comms-live-20260927-wzjtqhza/registry.json')]:
 try:
  value=json.loads(p.read_text())
  for row in value.get('threads',{}).values():
   if row.get('worktree'): protected.add(os.path.realpath(row['worktree']))
 except (OSError,ValueError): pass
proc=[]
for p in pathlib.Path('/proc').iterdir():
 if not p.name.isdigit(): continue
 try:
  if p.stat().st_uid!=os.getuid(): continue
  cwd=os.readlink(p/'cwd'); raw=(p/'cmdline').read_bytes().replace(b'\0',b' ').decode(errors='replace')
  # Store no command text, settings or credentials in the report.
  proc.append((p.name,cwd,raw))
 except OSError: continue
open_branches={}
for repo in ['agent-comms','toad','textual']:
 code,raw=cmd(['gh','pr','list','--repo','OpenHCSDev/'+repo,'--state','open','--limit','200','--json','number,headRefName'])
 if code: raise RuntimeError('Cannot establish open PR ownership: '+repo)
 open_branches[repo]={r['headRefName']:r['number'] for r in json.loads(raw)}

def inspect(p):
 r={'path':str(p)}
 if not (p/'.git').exists(): return dict(r,decision='not-git')
 code,top=cmd(['git','rev-parse','--show-toplevel'],p)
 if code or top!=str(p): return dict(r,decision='not-worktree-root')
 _,remote=cmd(['git','remote','get-url','origin'],p)
 repo=next((name for name in open_branches if remote.rstrip('/').endswith('/'+name+'.git')),None)
 if not repo: return dict(r,decision='other-repository')
 r['repo']=repo
 _,r['branch']=cmd(['git','branch','--show-current'],p)
 _,r['head']=cmd(['git','rev-parse','HEAD'],p)
 _,common=cmd(['git','rev-parse','--path-format=absolute','--git-common-dir'],p);r['common']=common
 if os.path.realpath(p) in protected: return dict(r,decision='registered-owner')
 if r['branch'] in open_branches[repo]: return dict(r,decision='open-pr',pr=open_branches[repo][r['branch']])
 active=[pid for pid,cwd,raw in proc if cwd==str(p) or cwd.startswith(str(p)+'/') or str(p)+'/' in raw or (' '+str(p)+' ') in raw]
 if active: return dict(r,decision='process-reference',pids=active)
 code,status=cmd(['git','status','--porcelain=v1','--untracked-files=all'],p)
 if code or status: return dict(r,decision='dirty',status_lines=len(status.splitlines()))
 code,ignored=cmd(['git','ls-files','--others','--ignored','--exclude-standard','--directory'],p)
 # Only generated cache/build environments may be silently discarded by git.
 safe={'.venv/','venv/','__pycache__/','.pytest_cache/','.mypy_cache/','.ruff_cache/','build/','dist/','node_modules/','.uv-cache/','.coverage','htmlcov/','.tox/','.nox/'}
 unsafe=[a for a in ignored.splitlines() if not any(a==s or a.startswith(s) or ('/__pycache__/' in a) for s in safe)]
 if unsafe: return dict(r,decision='ignored-data',ignored=unsafe[:15])
 code,_=cmd(['git','merge-base','--is-ancestor',r['head'],'refs/remotes/origin/main'],p)
 if code: return dict(r,decision='not-merged-into-local-origin-main')
 _,listing=cmd(['git','worktree','list','--porcelain'],p)
 block=next((b for b in listing.split('\n\n') if b.startswith('worktree '+str(p)+'\n')), '')
 if not block or '\nlocked' in block: return dict(r,decision='locked-or-unregistered')
 _,sz=cmd(['du','-sx','--block-size=1',str(p)])
 r['bytes']=int(sz.split()[0]);r['decision']='removable';return r
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
 rows=list(pool.map(inspect,sorted(p for p in base.iterdir() if p.is_dir())))
result={'created_at':time.time(),'registered_paths_protected':len(protected),'rows':rows}
(out/'audit.json').write_text(json.dumps(result,indent=2)+'\n')
from collections import Counter
print(json.dumps({'counts':dict(Counter(r['decision'] for r in rows)),'removable_bytes':sum(r.get('bytes',0) for r in rows if r['decision']=='removable'),'removable':[(r['path'],r['bytes']) for r in rows if r['decision']=='removable']},indent=2))
