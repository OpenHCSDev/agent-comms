from pathlib import Path
import json,hashlib,zipfile,sys
from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_tables.executions import ExecutionRecord
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.coordination_tables.attempts import AttemptRecord
from agent_comms.field_codec import FieldCodec
import agent_comms.field_codec as codec
artifact=Path(__file__).resolve().parent
prefix=Path(sys.prefix)
assert prefix == Path('/home/ts/wt/toad-viewport-raster-cpu-continuation-20261001/.artifacts/runtime-native-style22-20261002')
assert Path(codec.__file__).is_relative_to(prefix)
wheel=artifact/'candidate-wheel/agent_comms-0.1.0-py3-none-any.whl'
site=prefix/'lib/python3.14/site-packages'
verified=[]
with zipfile.ZipFile(wheel) as z:
 for name in z.namelist():
  if name.startswith('agent_comms/') and not name.endswith('/'):
   raw=z.read(name)
   assert (site/name).read_bytes()==raw,name
   verified.append(name)
root=Path('/var/tmp/agent-comms-live-20260927-wzjtqhza')
observations=[]
for owner,path in [(ExecutionRecord,root/'coordination.sqlite3'),(AttemptRecord,root/'coordination.sqlite3'),(WakeAssignment,root/'coordination.sqlite3')]:
 with CoordinationStore.observing(path,lock_timeout=1) as db:
  assert db.execute('PRAGMA query_only').fetchone()[0]==1
  rows=owner.read(db.execute('SELECT '+owner._column_list(owner.columns())+' FROM "'+owner.declared_name+'" LIMIT ?', (50,)))
 assert len(rows)==50,(owner.__name__,len(rows))
 values=[]
 for row in rows:
  projected=FieldCodec.project(row,'snapshot')
  for name,descriptor in FieldCodec._projections(type(row),'snapshot').items():
   assert projected[descriptor.wire_name]==FieldCodec.project(getattr(row,name),'snapshot')
  values.append(json.dumps(projected,sort_keys=True))
 assert len(set(values))==50
 observations.append({'owner':owner.__name__,'rows':len(rows),'distinct_projections':len(set(values))})
result={'state':'PASS installed original typed-row projection from actual query-only public databases','installed_codec':codec.__file__,'prefix':str(prefix),'source_head':'87dca5aa','wheel_sha256':hashlib.sha256(wheel.read_bytes()).hexdigest(),'installed_package_files_byte_equal':len(verified),'tables':observations,'cache':FieldCodec._projections.cache_info()._asdict(),'public_writes':0,'provider_calls':0,'ui_claim':False,'speed_claim':False}
(artifact/'installed-read.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2))
