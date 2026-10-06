from pathlib import Path
import os,json,stat,subprocess,hashlib,time
OUT=Path(__file__).parent
CANDIDATES=[Path('/home/ts/.local/share/agent-comms')/name for name in ['runtime-body-readiness-20260930','runtime-owner-cli-config-20260930','runtime-all-merged-20260930','runtime-sidebar-native-custody-20260930','runtime-c3-reviewed-pair-20260930','runtime-atomic-page-handoff-20261001','runtime-native-publication-joined-20261001']]
needles=[str(p).encode() for p in CANDIDATES]
report={'time_ns':time.time_ns(),'candidates':[],'process_refs':[],'process_scan_errors':[],'launcher_refs':[],'metadata_refs':[],'donor_refs':[]}
def matches(b):return [str(p) for p,n in zip(CANDIDATES,needles) if n in b]
for p in CANDIDATES:
 files=[];seen=set();blocks=exclusive=0;links=0
 for d,ds,fs in os.walk(p):
  for name in fs:
   f=Path(d)/name;s=f.lstat();key=(s.st_dev,s.st_ino)
   if key in seen:continue
   seen.add(key);blocks+=s.st_blocks*512
   if s.st_nlink==1:exclusive+=s.st_blocks*512
   else:links+=1
   if f.suffix in ('.jsonl','.sqlite','.db') or f.name in ('registry.json','input_dispositions.json','goals.json'):
    files.append(str(f))
 report['candidates'].append({'path':str(p),'allocated_unique_inode_bytes':blocks,'single_link_file_bytes':exclusive,'multiply_linked_files':links,'semantic_data_files':files})
for p in Path('/proc').iterdir():
 if not p.name.isdigit() or p.stat().st_uid != os.getuid() or int(p.name)==os.getpid():continue
 try:
  state=(p/'stat').read_text().split(') ',1)[1].split()[0]
  if state=='Z':continue
 except (FileNotFoundError,ProcessLookupError):continue
 try:
  for kind in ['cmdline','environ','maps']:
   for target in matches((p/kind).read_bytes()):report['process_refs'].append({'pid':int(p.name),'kind':kind,'target':target})
  for f in [p/'exe',p/'cwd',p/'root',*list((p/'fd').iterdir())]:
   try:
    for target in matches(os.readlink(f).encode()):report['process_refs'].append({'pid':int(p.name),'kind':str(f.relative_to(p)),'target':target})
   except FileNotFoundError:pass
 except (FileNotFoundError,ProcessLookupError):pass
 except PermissionError as e:report['process_scan_errors'].append({'pid':int(p.name),'type':type(e).__name__,'comm':(p/'comm').read_text().strip(),'stat_state':state})
for root in [Path('/home/ts/bin'),Path('/home/ts/.local/bin'),Path('/home/ts/.agent-comms/stack/bin')]:
 for f in root.iterdir():
  data=str(f.resolve()).encode() if f.is_symlink() else f.read_bytes() if f.is_file() else b''
  for target in matches(data):report['launcher_refs'].append({'file':str(f),'target':target})
route=Path('/home/ts/.local/state/agent-comms/active-route.json')
report['route_sha256']=hashlib.sha256(route.read_bytes()).hexdigest()
roots=[Path('/home/ts/.agent-comms')]
r=json.loads(route.read_text())
def collect(v):
 if isinstance(v,dict):
  for k,x in v.items():
   if isinstance(x,str) and x.startswith('/') and k in ('root','wire_root'):roots.append(Path(x))
   collect(x)
 elif isinstance(v,list):
  for x in v:collect(x)
collect(r)
report['route_root_keys']=[str(x) for x in roots]
for f in [route,*[f for root in roots for f in root.glob('*.json')],*Path('/home/ts/.agent-comms/.pi').glob('*.json'),*Path('/home/ts/.config/agent-comms').glob('*.json')]:
 for target in matches(f.read_bytes()):report['metadata_refs'].append({'file':str(f),'target':target})
# Query dependency/donor declarations only, preserving source/proof/history content.
args=['rg','-l','-F']
for n in needles:args+=['-e',n.decode()]
args+=['/home/ts/wt','/home/ts/.local/share/agent-comms','--glob','*.pth','--glob','pyvenv.cfg','--hidden','--no-ignore']
p=subprocess.run(args,capture_output=True,text=True)
report['donor_refs']=p.stdout.splitlines();report['donor_scan_exit']=p.returncode;report['donor_scan_error']=p.stderr
report['safe_to_remove']=not any(report[k] for k in ['process_refs','process_scan_errors','launcher_refs','metadata_refs','donor_refs']) and p.returncode in (0,1) and not any(x['semantic_data_files'] for x in report['candidates'])
(OUT/'retired-after-applied-cohort-reference-audit.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
