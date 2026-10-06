from pathlib import Path
import os,json,stat,collections,hashlib,tarfile,shutil,time
OUT=Path(__file__).parent
BASE=Path('/home/ts/.local/share/agent-comms')
NAMES=['runtime-body-readiness-20260930','runtime-owner-cli-config-20260930','runtime-all-merged-20260930','runtime-sidebar-native-custody-20260930','runtime-c3-reviewed-pair-20260930']
ROOTS=[BASE/n for n in NAMES]
audit=json.loads((OUT/'retired-after-applied-cohort-reference-audit.json').read_text())
assert time.time_ns()-audit['time_ns']<600*10**9
assert all(not audit[k] for k in ('process_refs','launcher_refs','metadata_refs','donor_refs'))
assert audit['donor_scan_exit']==1 and not audit['donor_scan_error']
assert all(x['comm'] in ('systemd','(sd-pam)') for x in audit['process_scan_errors'])
assert all(not x['semantic_data_files'] for x in audit['candidates'])
reverse=json.loads((OUT/'retired-prefix-reverse-git.json').read_text())
assert reverse=={'exit':1,'matches':[],'stderr':''}
# lsof matches inode aliases, even when actual mapped pathname is another prefix.
# Preserve original output and require every match to be a current-live memory map.
resolutions={r['pid']:r for r in json.loads((OUT/'retired-prefix-lsof-inode-resolution.json').read_text())}
lsof=json.loads((OUT/'retired-prefix-lsof.json').read_text())
live=BASE/'runtime-native-applied-cohort-20261001'
for row in lsof:
 assert not row['stderr']
 for line in row['stdout'].splitlines()[1:]:
  fields=line.split();pid=int(fields[1]);old=Path(fields[-1]);r=resolutions[pid]
  assert fields[3]=='mem' and str(live/'bin/python') in r['argv']
  current=live/old.relative_to(BASE/row['name'])
  assert current.stat().st_ino==old.stat().st_ino and current.stat().st_dev==old.stat().st_dev
  assert r['maps'] and all(str(current) in m for m in r['maps'])
route=Path('/home/ts/.local/state/agent-comms/active-route.json')
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def protected():
 return {'route':sha(route),'live_activation':sha(live/'activation.json'),'default_targets':{p.name:str(p.resolve()) for p in Path('/home/ts/.local/bin').iterdir() if p.name in ('agent-comms-acp','agent-comms','toad','toad-comms','pi-comms')}}
protection=protected()
metadata=[];counts=collections.Counter();inodes={};dirs=0;identities={}
for root in ROOTS:
 assert root.is_dir() and not root.is_symlink() and root.resolve()==root
 identities[str(root)]=(root.stat().st_dev,root.stat().st_ino)
 for d,ds,fs in os.walk(root):
  assert not any(n in ('.git','.gitmodules','gitdir','commondir','native-journals','.release-private') for n in ds+fs)
  dirs+=Path(d).lstat().st_blocks*512
  for n in fs+ [x for x in ds if (Path(d)/x).is_symlink()]:
   p=Path(d)/n;s=p.lstat();assert s.st_uid==os.getuid()
   key=(s.st_dev,s.st_ino);counts[key]+=1;inodes[key]=s
   if p.parent==root or 'bin' in p.relative_to(root).parts[:1] or any(x.endswith('.dist-info') for x in p.parts):
    metadata.append(p)
manifest=[]
for p in metadata:
 s=p.lstat();manifest.append({'path':str(p.relative_to(BASE)),'mode':s.st_mode,'size':s.st_size,'mtime_ns':s.st_mtime_ns,'sha256':sha(p) if p.is_file() and not p.is_symlink() else None,'symlink':os.readlink(p) if p.is_symlink() else None})
archive=OUT/'retired-published-prefix-metadata.tar.gz'
assert not archive.exists()
with tarfile.open(archive,'w:gz',dereference=False) as t:
 for p in metadata:t.add(p,arcname=str(p.relative_to(BASE)),recursive=False)
os.chmod(archive,0o600)
with tarfile.open(archive,'r:gz') as t:
 members=t.getmembers();assert len(members)==len(manifest)
 for member,row in zip(members,manifest):
  assert member.name==row['path']
  if row['sha256']:
   assert hashlib.sha256(t.extractfile(member).read()).hexdigest()==row['sha256']
  elif row['symlink'] is not None:assert member.linkname==row['symlink']
(OUT/'retired-published-prefix-metadata-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
file_reclaim=sum(s.st_blocks*512 for k,s in inodes.items() if counts[k]==s.st_nlink)
shared=sum(s.st_blocks*512 for k,s in inodes.items() if counts[k]<s.st_nlink)
receipt={'status':'removing','time_ns':time.time_ns(),'removed_paths':[],'attributable_unlinked_blocks_bytes':file_reclaim+dirs,'directory_blocks_bytes':dirs,'shared_blocks_not_claimed_bytes':shared,'metadata_archive':str(archive.resolve()),'metadata_archive_sha256':sha(archive),'metadata_entries_verified':len(manifest),'metadata_archive_allocated_bytes':archive.stat().st_blocks*512,'before_home_available_bytes':shutil.disk_usage('/home/ts').free,'protected_before':protection,'process_census_qualification':'All accessible application/native consumers inspected; two systemd/sd-pam services deny proc descriptor/environment access. lsof inode-alias positives resolved to current live mappings; their live inode links remain.','historical_references':'Recorded completed staging/operator/capture scripts retained; Heisenberg confirmed no current254/472 dependencies. Sch current gate uses live prefix. Recent negative atomic-page-handoff/native-publication-joined prefixes retained.','source_worktrees_native_history_unknown_deleted':False}
path=OUT/'retired-published-prefix-removal.json'
def save():path.write_text(json.dumps(receipt,indent=2)+'\n')
save()
assert protected()==protection
for root in ROOTS:
 assert (root.stat().st_dev,root.stat().st_ino)==identities[str(root)]
 shutil.rmtree(root)
 receipt['removed_paths'].append(str(root));save()
assert all(not p.exists() for p in ROOTS)
assert protected()==protection
# Live shared mapped library retains its exact inode via independent hardlinks.
for row in lsof:
 for line in row['stdout'].splitlines()[1:]:
  fields=line.split();old=Path(fields[-1]);current=live/old.relative_to(BASE/row['name'])
  assert current.stat().st_ino==int(fields[-2])
receipt.update(status='completed',finished_ns=time.time_ns(),after_home_available_bytes=shutil.disk_usage('/home/ts').free,protected_after=protected(),live_route_activation_default_targets_unchanged=True)
receipt['actual_df_delta_bytes']=receipt['after_home_available_bytes']-receipt['before_home_available_bytes']
receipt['net_attributable_recovery_bytes']=receipt['attributable_unlinked_blocks_bytes']-receipt['metadata_archive_allocated_bytes']
receipt['df_qualification']='Actual df delta is concurrent-system observation; hardlink-aware block accounting minus retained metadata archive is attributable recovery.'
save();print(json.dumps(receipt,indent=2))
