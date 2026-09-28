import asyncio,json,sys,time,os
from dataclasses import replace
from pathlib import Path
from agent_comms.child_process import *
async def run():
    print('identity', ProcessIdentity.capture(os.getpid()),flush=True)
    result=await BoundedRun.run((sys.executable,'-c','print("Windows real execution")'),timeout=5)
    assert result.outcome.successful,(result.outcome,result.stderr)
    print('ordinary', result.stdout.decode().strip(),flush=True)
    child=DetachedProcess.launch((sys.executable,'-c','import time; time.sleep(60)'))
    try:
        stale=DetachedProcess.attach(replace(child.identity,start_time=child.identity.start_time+1))
        assert not stale.alive()
        try: stale.force()
        except IdentityMismatchError: pass
        else: raise AssertionError('stale identity signaled')
        assert child.alive()
    finally: child.stop_sync()
    assert not child.alive()
    print('detached identity refusal and retirement passed',flush=True)
    captured=[]
    def refuse(identity):
        captured.append(identity)
        raise ValueError('reservation refused')
    try:
        DetachedProcess.launch((sys.executable,'-c','raise RuntimeError("must not execute")'),before_start=refuse)
    except ValueError as error: assert str(error)=='reservation refused'
    else: raise AssertionError('reservation accepted')
    assert len(captured)==1 and not captured[0].alive()
    print('failed Windows reservation reaped suspended child',flush=True)
    receipt=Path(sys.argv[1])
    code='''import subprocess,sys,time,os,json
from pathlib import Path
from agent_comms.child_process import ProcessIdentity
from agent_comms.field_codec import FieldCodec
child=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGBREAK,signal.SIG_IGN); print("ready",flush=True); time.sleep(60)'],stdout=subprocess.PIPE)
assert child.stdout.readline().strip()==b'ready'
Path(sys.argv[1]).write_text(json.dumps([FieldCodec.encode(ProcessIdentity.capture(p)) for p in (os.getpid(),child.pid)]))
time.sleep(60)
'''
    result=await BoundedRun.run((sys.executable,'-c',code,str(receipt)),timeout=2)
    from agent_comms.field_codec import FieldCodec
    members=[FieldCodec.decode(ProcessIdentity,v) for v in json.loads(receipt.read_text())]
    assert isinstance(result.outcome,TimedOutOutcome),result.outcome
    assert all(not m.alive() for m in members),members
    print('bounded real child and TERM-resistant grandchild both retired',flush=True)
    receipt.unlink()
    attached=await AttachedChild.start((sys.executable,'-c',code,str(receipt)))
    try:
        async with asyncio.timeout(5):
            while not receipt.exists(): await asyncio.sleep(.02)
        members=[FieldCodec.decode(ProcessIdentity,v) for v in json.loads(receipt.read_text())]
        await attached.stop()
        assert all(not m.alive() for m in members),members
        print('attached real child and grandchild both retired',flush=True)
    finally: await attached.stop()
asyncio.run(run())
