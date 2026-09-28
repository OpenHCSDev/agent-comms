"""Actual owned worker/ACP/SQLite/copy-install exercise; no provider input."""
import json
from contextlib import closing
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import tempfile
import time

from agent_comms.comms import Comms
from agent_comms.threads import Thread
from agent_comms.input_disposition import InputDispositions
from agent_comms.compaction_journal import CompactionJournal

TREE = Path(__file__).resolve().parents[2]
OLD = Path('/home/ts/.local/share/agent-comms/runtime-peer-close-20260928')
NEW = Path('/home/ts/.local/share/agent-comms/runtime-round2-20260928')
OLD_PI = Path('/home/ts/.local/share/agent-comms/native-compaction-policy-b3a9c06/node_modules/@earendil-works/pi-coding-agent')
NEW_PI = Path('/home/ts/.local/share/agent-comms/native-current-689ce4b5d0592b9a/node_modules/@earendil-works/pi-coding-agent')
OP = TREE / 'tools/cutover/quiet_owners.py'

def wait_for(predicate):
    until = time.monotonic() + 8
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError('bounded wait expired')

def run(runtime, *args, success=True):
    result = subprocess.run([str(runtime/'bin/python'), '-I', *map(str,args)], capture_output=True, text=True, timeout=20)
    if success and result.returncode:
        raise AssertionError(result.stderr)
    if not success:
        assert result.returncode != 0, result.stdout
    return result

with tempfile.TemporaryDirectory(prefix='owned-d22-quiet-', dir='/var/tmp') as directory:
    area = Path(directory)
    root = area / 'root'
    root.mkdir(mode=0o700)
    project = area / 'project'
    project.mkdir()
    # Genuine native session, created without opening a provider/model.
    native = subprocess.run(['node', '--input-type=module', '-e', f'''
import {{SessionManager}} from {json.dumps((OLD_PI/'dist/core/session-manager.js').as_uri())};
const s = SessionManager.create({json.dumps(str(project))}, {json.dumps(str(area/'sessions'))});
s.appendMessage({{role:'user',content:'retained fixture',timestamp:Date.now()}});
s.appendMessage({{role:'assistant',content:[{{type:'text',text:'saved reply'}}],api:'anthropic-messages',provider:'anthropic',model:'fixture',usage:{{input:0,output:0,cacheRead:0,cacheWrite:0,cost:{{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}}},stopReason:'stop',timestamp:Date.now()}});
console.log(s.getSessionFile());
'''], capture_output=True, text=True, timeout=10)
    assert native.returncode == 0, native.stderr
    session = Path(native.stdout.strip())
    session_before = session.read_bytes()
    comms = Comms(root)
    comms.threads.register(Thread('fixture', frozenset(), str(project), session_file=str(session), model='anthropic/claude-sonnet-4-5', thinking_level='off'))
    root_id = comms.messaging.initialize_private_initial_protocol()
    comms.messaging.initialize_private_claim_protocol()
    comms.messaging.send('fixture', '#all', 'Retained wire history; no other recipients')
    route = area / 'route.json'
    route_value = dict(version=1,root=str(root),wire_root_id=root_id,native_package=str(OLD_PI))
    route.write_text(json.dumps(route_value)); route.chmod(0o600)
    comms.owners.pin_private_nk_launch(root,root_id,OLD_PI)
    launched = comms.owners.start('fixture', agent_bin='pi')
    worker = launched.pid
    acp = None
    restarted = []
    try:
        wait_for(lambda: comms.owners._is_local_participant(comms.registry.require('fixture'),wait=False))
        time.sleep(6)
        inputs = InputDispositions(root/'input_dispositions.json')
        inputs.record('retained-unknown', seq=None, owner='fixture', admission=comms.registry.snapshot().admission_generations['fixture'],target='fixture',text='Never replay')
        journal = CompactionJournal(root/'compaction-commits.sqlite3')
        journal.reserve_private_raw_input(session,'a'*32)
        def raw_unknown():
            with closing(sqlite3.connect(root/'compaction-commits.sqlite3')) as db:
                return db.execute('SELECT * FROM private_raw_inputs').fetchall()
        original_raw = raw_unknown()
        receipt = area/'stop.json'
        flags = ['--runtime',OLD,'--root',root,'--receipt',receipt,'--route-file',route]
        # Real ACP process selected on this owned root. Operator must not fence it.
        env = dict(os.environ,AGENT_COMMS_ROOT=str(root))
        acp = subprocess.Popen([str(OLD/'bin/python'),'-I','-m','agent_comms.acp'],env=env,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        time.sleep(.25)
        before = (root/'registry.json').read_bytes()
        refused = run(OLD,OP,*flags,success=False)
        assert 'still attached' in refused.stderr, refused.stderr
        assert not receipt.exists() and not (root/'.maintenance-enabled').exists()
        assert (root/'registry.json').read_bytes() == before
        acp.terminate(); acp.wait(timeout=5); acp=None
        time.sleep(1)
        
        stopped = run(OLD,OP,*flags)
        stop = json.loads(stopped.stdout)
        assert stop['stopped'] == ['fixture'], stop
        os.waitpid(worker,0)
        wait_for(lambda: not comms.owners._process_alive(worker))
        assert session.read_bytes() == session_before and raw_unknown() == original_raw
        assert comms.owners.maintenance.read().phase == 'installing'
        # Actual current converter plus atomic exchange on this owned old root.
        code = f'''
import sys,json
from pathlib import Path
sys.path.insert(0,{str(TREE/'tools/cutover')!r})
from current_root import stage,stage_attached_history
from install_root import install,assert_quiet
source=Path({str(root)!r}); candidate=Path({str(area/'candidate')!r})
assert_quiet(source)
stage(source,candidate)
stage_attached_history(source,candidate)
install(source,candidate,Path({str(area/'backup')!r}))
print('converted-and-installed')
'''
        converted=run(NEW,'-c',code)
        route_value['native_package']=str(NEW_PI)
        route.write_text(json.dumps(route_value)); route.chmod(0o600)
        restart=run(NEW,OP,'--restart','--runtime',NEW,'--root',root,'--receipt',receipt,'--route-file',route,'--native-package',NEW_PI)
        result=json.loads(restart.stdout)
        restarted=[row['pid'] for row in result['restart_requested']]
        gate=json.loads((root/'.maintenance-state').read_text())
        assert gate['phase']=='ready' and gate['nonce']==stop['maintenance']['nonce'] and gate['generation']==stop['maintenance']['generation']
        assert session.read_bytes()==session_before
        assert raw_unknown()==original_raw
        assert json.loads((root/'input_dispositions.json').read_text())==stop['input_outcomes']
        time.sleep(.75)
        assert all(Path(f'/proc/{pid}').exists() for pid in restarted)
        print(json.dumps(dict(client_refusal=True,old_worker_stopped=True,actual_copy_install=True,same_gate_reopened=True,new_worker_started=True,session_bytes_preserved=True,original_unknown_preserved=True,raw_unknown_preserved=True,provider_inputs=0),indent=2))
    finally:
        if acp is not None:
            acp.terminate(); acp.wait(timeout=5)
        for pid in [worker,*restarted]:
            try:
                environment=Path(f'/proc/{pid}/environ').read_bytes()
                if f'AGENT_COMMS_ROOT={root}'.encode() in environment.split(b'\0'):
                    os.kill(pid,signal.SIGTERM)
            except (FileNotFoundError,PermissionError):
                pass
        time.sleep(.3)
