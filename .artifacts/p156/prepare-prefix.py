"""Prepare only the authorized private contiguous prefix; never start an owner."""
import json, os, shutil, sqlite3, time
from dataclasses import replace
from pathlib import Path
from agent_comms.bus_page_index import BusPageIndex, BusPageRow
from agent_comms.field_codec import FieldCodec
from agent_comms.private_path import FileRevision
from agent_comms.wire_log import WireLog, OpenedWireSnapshot
from agent_comms.wire_metadata import WireMetadata
s=Path('/var/tmp/agent-scratch-p156')
original=Path('/var/tmp/agent-comms-live-20260927-wzjtqhza')
p=s/'bus.jsonl'
assert p.stat().st_size == 2*1024**3
with p.open('r+b') as f:
    end=f.seek(0,2)
    while end:
        start=max(0,end-1024*1024); f.seek(start); chunk=f.read(end-start)
        pos=chunk.rfind(b'\n')
        if pos>=0:
            boundary=start+pos+1; f.truncate(boundary); break
        end=start
os.chmod(p,0o600)
marker=FieldCodec.decode(WireMetadata,json.loads((original/'bus_meta.json').read_text()))
started=time.perf_counter()
with p.open('rb') as f, BusPageIndex(p) as index:
    opened=OpenedWireSnapshot(marker,0,f,FileRevision.from_file(f))
    index.prepare(opened)
    with index.snapshot():
        rows,last=index.connection.execute(f'SELECT COUNT(*),MAX(seq) FROM {BusPageRow.declared_name}').fetchone()
index_seconds=time.perf_counter()-started
marker=replace(marker,last_seq=last,admission_after_seq=min(marker.admission_after_seq,last),checkpoint_version=None,checkpoint_seal=None)
WireLog(p).write_metadata_unlocked(marker)
for name in ('registry.json','catalog.json','read_ledger.json'):
    shutil.copyfile(original/name,s/name); os.chmod(s/name,0o600)
with sqlite3.connect('file:'+str(original/'coordination.sqlite3')+'?mode=ro',uri=True) as src, sqlite3.connect(s/'coordination.sqlite3') as dst:
    src.backup(dst)
os.chmod(s/'coordination.sqlite3',0o600)
receipt=dict(bytes=boundary,public_rows=rows,last_seq=last,index_seconds=index_seconds,source=str(original/'bus.jsonl'),root=str(s))
(s/'prefix-receipt.json').write_text(json.dumps(receipt,indent=2))
print(json.dumps(receipt),flush=True)
# The normal canonical writer barrier builds the private prefix certificate.
with WireLog(p).locked():
    pass
print('canonical prefix certificate ready',flush=True)
