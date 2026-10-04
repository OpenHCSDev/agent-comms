import hashlib,json,os,sqlite3,sys,time
from contextlib import ExitStack,closing
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tools/cutover'))
from agent_comms.field_codec import FieldCodec
from agent_comms.private_sidecar import _locked_directory,_read_snapshot
from agent_comms.private_path import PrivateDirectoryRole
from native_schema_carry import NativeSchemaDeclaration,RuntimeNativeFiles,PromptBindingNativeStore
from retained_summary_reset import RuntimeGoalFiles
from publish_openhcs_recovery import ROOT,fsync_directory,digest
from routing_recovery import write_original
import agent_comms
base=Path(__file__).parent
raw=base/'public-raw-preimages';raw.mkdir(mode=0o700)
copy=base/'private-original';copy.mkdir(mode=0o700)
source=NativeSchemaDeclaration.observe()
write_original(base/'source-declaration.json',(json.dumps(FieldCodec.encode(source))+'\n').encode())
assert source.release_versions==(9,3,3,5)
start=time.monotonic()
with ExitStack() as custody:
 # The existing lock was observed before this call. No new public lock file,
 # writer/store initialization, snapshot publication or admission occurs.
 directory=custody.enter_context(_locked_directory(ROOT/PromptBindingNativeStore.name,blocking=False))
 binding_snapshot=_read_snapshot(directory,PromptBindingNativeStore.name)
 if binding_snapshot is None:raise RuntimeError('Existing binding missing')
 native=custody.enter_context(RuntimeNativeFiles(ROOT).acquire())
 goals=custody.enter_context(RuntimeGoalFiles(ROOT).acquire())
 held=(*native.originals,*goals.originals)
 if sum(item.revision.size for item in held)>32*1024*1024:raise RuntimeError('Capture exceeds owned32MiB bound')
 connections=[]
 for item in held:
  db=custody.enter_context(closing(sqlite3.connect(item.path.as_uri()+'?mode=ro',uri=True,isolation_level=None,timeout=0.1)))
  db.execute('PRAGMA query_only=ON')
  if db.execute('PRAGMA journal_mode').fetchone()!=('delete',):raise RuntimeError('Requires existing rollback format')
  db.execute('BEGIN')
  db.execute('SELECT COUNT(*) FROM sqlite_master').fetchone() # acquire committed SHARED read
  connections.append((item,db))
 # All rollback readers and the binding lock now overlap. Never open/close
 # another original inode FD here: that releases this process SQLite locks.
 # Existing held FD checks/pread preserve those locks and exact bytes.
 for item,db in connections:
  item.require_original()
  relative=item.path.relative_to(ROOT)
  raw_target=raw/relative;target=copy/relative
  raw_target.parent.mkdir(mode=0o700,exist_ok=True);target.parent.mkdir(mode=0o700,exist_ok=True)
  remaining=item.revision.size;parts=[];offset=0
  while remaining:
   chunk=os.pread(item.descriptor,min(remaining,1024*1024),offset)
   if not chunk:raise RuntimeError('Original source truncated')
   parts.append(chunk);offset+=len(chunk);remaining-=len(chunk)
  value=b''.join(parts)
  if hashlib.sha256(value).hexdigest()!=item.sha256:raise RuntimeError('Original changed before read cohort')
  write_original(raw_target,value)
  write_original(target,b'')
  with closing(sqlite3.connect(target)) as backup:
   backup.execute('PRAGMA synchronous=FULL');db.backup(backup)
  with target.open('rb') as saved:os.fsync(saved.fileno())
 for item,db in connections:item.require_original()
 directory.unchanged(PromptBindingNativeStore.name,binding_snapshot[1])
 evidence=[item.evidence() for item in held]
 end=time.monotonic()
 # Closing SQLite readers precedes closing separately held original FDs.
 # ExitStack enters readers last, so releases them first.
for folder in (raw/'goal-private',copy/'goal-private',raw,copy,base):fsync_directory(folder)
receipt={'classification':'consistent-readonly-current-public9ccc-cohort-not-public-stopped-grant',
 'root':str(ROOT),'source_package':agent_comms.__file__,'interpreter':sys.executable,
 'source_versions':list(source.release_versions),'source_declaration_sha256':digest(base/'source-declaration.json'),
 'originals':evidence,'private_logical_backups':{str(p.relative_to(copy)):digest(p) for p in copy.rglob('*.sqlite3')},
 'raw_preimages':str(raw),'private_root':str(copy),'read_custody_seconds':end-start,
 'consistency':'overlapping rollback SHARED SQLite snapshots plus existing atomic-binding snapshot lock; held-FD original revision and exact byte hashes verified before release',
 'public_stops':0,'public_writes':0,'provider_calls':0,'input_replays':0}
write_original(base/'capture-receipt.json',(json.dumps(receipt,indent=2)+'\n').encode())
print(json.dumps({'classification':receipt['classification'],'files':len(held),'bytes':sum(i.revision.size for i in held),'read_custody_seconds':end-start,'source_versions':receipt['source_versions']}))
