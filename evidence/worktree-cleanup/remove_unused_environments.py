import pathlib,json,subprocess,os,shutil,re
out=pathlib.Path(__file__).parent
rows=json.loads((out/'audit.json').read_text())['rows']
registry_text=''
for p in ['/home/ts/.agent-comms/registry.json','/var/tmp/agent-comms-live-20260927-wzjtqhza/registry.json']:
 try: registry_text+=pathlib.Path(p).read_text()
 except OSError: pass
# Protect live launchers and editable installations as well as recorded agents.
references=[]
for base in ['/home/ts/.local/share/agent-comms','/home/ts/.local/bin','/home/ts/bin','/home/ts/wt']:
 for p in pathlib.Path(base).rglob('*'):
  try:
   if p.is_symlink(): references.append(str(p.resolve()))
   elif p.suffix=='.pth': references.append(p.read_text())
  except (OSError,UnicodeError): pass
proc=[]
for p in pathlib.Path('/proc').iterdir():
 if not p.name.isdigit():continue
 try:
  if p.stat().st_uid!=os.getuid():continue
  proc.append((os.readlink(p/'cwd'),(p/'cmdline').read_bytes()+(p/'environ').read_bytes()))
 except OSError:pass
records=[]
before=os.statvfs('/home/ts');free0=before.f_bavail*before.f_frsize
for row in rows:
 p=pathlib.Path(row['path'])
 if row['decision'] in {'open-pr','registered-owner','process-reference','other-repository','not-git','not-worktree-root'} or not p.exists():continue
 # Retain all worktree dependencies of registered owners even when stopped.
 if str(p) in registry_text or any(str(p) in ref for ref in references):continue
 if any(cwd==str(p) or cwd.startswith(str(p)+'/') or str(p).encode()+b'/' in raw for cwd,raw in proc):continue
 for name in ['.venv','.test-venv']:
  target=p/name
  if target.is_symlink() or not target.is_dir() or not (target/'pyvenv.cfg').is_file():continue
  tracked=subprocess.run(['git','ls-files','--',name],cwd=p,capture_output=True,text=True)
  if tracked.returncode or tracked.stdout.strip():continue
  size=int(subprocess.check_output(['du','-sx','--block-size=1',str(target)]).split()[0])
  # This deletes only an untracked generated Python environment, never its source or evidence.
  shutil.rmtree(target)
  records.append({'path':str(target),'bytes_estimate':size,'result':'removed-generated-environment'})
  (out/'environment-cleanup.json').write_text(json.dumps({'records':records},indent=2)+'\n')
after=os.statvfs('/home/ts');free1=after.f_bavail*after.f_frsize
receipt={'started_free_bytes':free0,'finished_free_bytes':free1,'free_bytes_change':free1-free0,'removed_bytes_estimate':sum(r['bytes_estimate'] for r in records),'records':records}
(out/'environment-cleanup.json').write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps({k:v for k,v in receipt.items() if k!='records'},indent=2));print('environments removed',len(records))
