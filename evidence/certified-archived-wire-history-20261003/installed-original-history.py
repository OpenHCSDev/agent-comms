"""Actual installed CLI and historical native readers; no input/provider call."""
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import agent_comms
from agent_comms.comms import wire
from agent_comms.historical_views import ChannelHistory, HistoricalMessage
from agent_comms.wire_log import WireLog

here = Path(__file__).resolve().parent
checkout = here.parents[1]
root = Path('/var/tmp/agent-comms-live-20260927-wzjtqhza')

def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

installed = Path(agent_comms.__file__).parent
assert installed.is_relative_to(Path(sys.prefix))
source = checkout / 'src/agent_comms'
source_proof = {str(p.relative_to(source)): digest(p) for p in source.rglob('*')
                if p.is_file() and '__pycache__' not in p.parts}
assert all(digest(installed / name) == value for name, value in source_proof.items())
(here/'installed-source-proof.json').write_text(json.dumps(source_proof, indent=2)+'\n')
comms = wire(root)
sources = comms.bus.history.sources()
protected = [root/'history_sources.json']
archive_inventory = []
for original in sources:
    original.validate()
    path = Path(original.root)
    protected.extend(path/name for name in ['bus.jsonl','bus_meta.json','private_bus_checkpoint.sqlite3',
                                          'bus_page_index.sqlite3','registry.json','catalog.json'])
    with WireLog(path/'bus.jsonl').certified_read() as opened:
        assert opened.connection.execute('PRAGMA query_only').fetchone()[0] == 1
        archive_inventory.append({'root':str(path), 'wire_root_id':opened.witness.root_id,
                                  'through_seq':opened.witness.through_seq,
                                  'bytes':opened.witness.offset,
                                  'index_tables':[r[0] for r in opened.connection.execute(
                                      "SELECT name FROM sqlite_master WHERE type='table'")],
                                  'declarations':len(original.provenance.threads),
                                  'aliases':len(original.provenance.aliases)})
native = next(t for t in comms.views.historical_threads() if t.thread.name=='nra-architecture')
protected.extend([Path(native.thread.session_file), Path(native.thread.session_file+'.input-proof')])
protected = [p for p in protected if p.is_file()]
before = {str(p):digest(p) for p in protected}
receipt = {'state':'running','head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=checkout,text=True).strip(),
           'prefix':sys.prefix,'python':sys.executable,'source_equal_files':len(source_proof),
           'direct_url':json.loads(metadata.distribution('agent-comms').read_text('direct_url.json')),
           'sdk':metadata.version('agent-client-protocol'),'root':str(root),'archives':archive_inventory,
           'protected_before':before,'provider_calls':0,'new_inputs':0,'replayed_inputs':0,
           'scope':'Current page, full ordinary channel CLI, original historical native transcript. No physical UI claim or schema rebuild.'}
(here/'installed-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
try:
    began=time.monotonic()
    current=comms.views.channel_history_page('#openhcs',limit=5)
    assert current.messages
    receipt['current_page']={'count':len(current.messages),'elapsed_s':time.monotonic()-began}
    env={k:v for k,v in os.environ.items() if not k.startswith('AGENT_COMMS_') and k!='PYTHONPATH'}
    began=time.monotonic()
    result=subprocess.run([str(Path(sys.prefix)/'bin/agent-comms'),'history','--channel','#openhcs'],
                          cwd=root,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=45)
    assert result.returncode==0,result.stderr.decode()[:2000]
    output=json.loads(result.stdout)
    assert output['channel']=='#openhcs' and output['messages']
    rows=output['messages']; archived=[r for r in rows if 'history' in r]
    receipt['full_cli']={'command':'agent-comms history --channel #openhcs', 'exit':result.returncode,
                         'elapsed_s':time.monotonic()-began,'count':len(rows),'archived_count':len(archived),
                         'output_bytes':len(result.stdout),'output_sha256':hashlib.sha256(result.stdout).hexdigest(),
                         'AGENT_COMMS_overrides':[]}
    historical=comms.bus.history.page(ChannelHistory('#comms',frozenset({'#comms'})),limit=5)
    assert historical.messages and all(isinstance(m,HistoricalMessage) for m in historical.messages)
    receipt['archived_page']={'count':len(historical.messages),'source':historical.messages[0].source.key,
                             'current_turn_authority':any(m.starts_turn for m in historical.messages)}
    assert not receipt['archived_page']['current_turn_authority']
    began=time.monotonic()
    page=comms.transcripts.thread_transcript_page(native.thread.name,historical_source=native.source.key)
    assert page.events and page.after.session_file==native.thread.session_file
    receipt['historical_native']={'name':native.thread.name,'source':native.source.key,
                                 'session_file':native.thread.session_file,'events':len(page.events),
                                 'elapsed_s':time.monotonic()-began}
    receipt['state']='passed'
finally:
    after={str(p):digest(p) for p in protected}
    receipt['protected_after']=after
    receipt['protected_unchanged']=before==after
    (here/'installed-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    assert before==after,'Original archive or native evidence changed'
print(json.dumps({k:v for k,v in receipt.items() if k in ('state','current_page','full_cli','archived_page','historical_native','protected_unchanged')}))
