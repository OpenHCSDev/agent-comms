"""Actual configured saved-fork preparation; no prompt and no provider invocation."""
from __future__ import annotations
import asyncio
import json
import os
import shutil
import select
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_comms import backend
from agent_comms.comms import Comms, wire
from agent_comms.native_arguments import NativeArguments
from agent_comms.native_fork import fork_native_session
from agent_comms.native_pi import NativePiRpcLaunch
from agent_comms.native_session_prepare import NativeSessionPreparation
from agent_comms.native_startup import NativeStartupAdmission
from agent_comms.session_fence import session_writer_fence
from agent_comms.child_process import ProcessIdentity
from agent_comms.threads import Thread
from contextlib import aclosing

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = Path(__file__).resolve().parent
SUFFIX = os.environ.get("PROBE_RECEIPT", "configured-preparation")
PACKAGE = Path('/home/ts/.local/share/agent-comms/native-current-d3967e8b6ee0cf28/node_modules/@earendil-works/pi-coding-agent')
COMMAND = '/home/ts/.local/share/agent-comms/runtime-context-observation-20260929/bin/pi-comms-native'

class ObservedPreparation(NativeSessionPreparation):
    async def initialize_rpc(self):
        self.marks.append({'phase':'get_state_written','at':time.monotonic()-self.begin})
        async for event in super().initialize_rpc():
            yield event
    async def receive_record(self):
        if self.loop_pause:
            pause, self.loop_pause = self.loop_pause, 0
            pipe = self.native.proc.process._transport.get_pipe_transport(1).get_extra_info("pipe")
            def observe_pipe():
                readable, _, _ = select.select([pipe], [], [], pause)
                self.marks.append({'phase':'native_stdout_ready' if readable else 'native_stdout_not_ready', 'at':time.monotonic()-self.begin})
            observer = threading.Thread(target=observe_pipe)
            observer.start()
            time.sleep(pause)
            observer.join(timeout=1)
        self.marks.append({'phase':'read_enter','at':time.monotonic()-self.begin})
        async for event in super().receive_record():
            yield event
        self.marks.append({'phase':'read_exit','at':time.monotonic()-self.begin,'bytes':len(getattr(self,'line',b''))})
    async def input_ready(self):
        self.marks.append({'phase':'attested','at':time.monotonic()-self.begin})
        await super().input_ready()

async def observe_loop(done, samples, begin, preparation):
    before=time.monotonic()
    while not done.is_set():
        await asyncio.sleep(.05)
        now=time.monotonic()
        sample={'elapsed':round(now-begin,3),'loop_gap':round(now-before,3)}
        before=now
        child=getattr(preparation,'native',None)
        if child is not None:
            pid=child.proc.identity.pid
            try:
                sample['pid']=pid
                sample['stat']=Path(f'/proc/{pid}/stat').read_text()
                sample['wait']=Path(f'/proc/{pid}/wchan').read_text()
            except OSError: pass
        samples.append(sample)

async def main():
    original=wire().registry.require('openhcs-pr159-viewer-bind-owner')
    before=Path(original.session_file).stat()
    receipt={'source_bytes':before.st_size,'model':original.model,'thinking':original.thinking_level,'prompts_sent':0,'attempts':[]}
    (ROOT/'.artifacts/native-cold-start').mkdir(exist_ok=True,parents=True)
    with TemporaryDirectory(prefix='configured-',dir=ROOT/'.artifacts/native-cold-start') as folder, TemporaryDirectory(prefix='comms-cold-probe-',dir='/var/tmp') as root_folder:
        config=Path(folder)/'pi';config.mkdir(mode=0o700)
        for leaf in ('auth.json','settings.json','models.json'):
            source=Path.home()/'.pi/agent'/leaf
            if source.exists():
                shutil.copyfile(source,config/leaf);(config/leaf).chmod(0o600)
        for key in ('PI_PROMPT','PI_AGENT_ID','PI_PARENT_ID','PI_TASK','AGENT_COMMS_THREAD','AGENT_COMMS_STARTUP_INPUT_KEY'):
            os.environ.pop(key,None)
        c=Comms(Path(root_folder)/'wire');rid=c.messaging.initialize_private_initial_protocol()
        os.environ.update(PI_CODING_AGENT_DIR=str(config),AGENT_COMMS_NATIVE_CONFIG_DIR=str(config),AGENT_COMMS_ROOT=str(c.root),AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=rid,AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(PACKAGE))
        fork=await asyncio.to_thread(fork_native_session,original.session_file,original.worktree,COMMAND)
        receipt['fork_bytes']=Path(fork.session_file).stat().st_size
        name='native-startup-diagnostic'
        c.threads.register(Thread(name,frozenset(),original.worktree,session_file=fork.session_file,model=original.model,thinking_level=original.thinking_level,process_identity=ProcessIdentity.capture(os.getpid())))
        env=dict(os.environ,AGENT_COMMS_THREAD=name,PI_AGENT_ID=name,AGENT_COMMS_MANAGED='1',PI_TIMING='1',PI_WORKTREE=original.worktree)
        arguments=NativeArguments.parse(['--no-tools','--no-extensions','--no-skills','--no-context-files','--no-prompt-templates']).with_model(original.model).with_thinking(original.thinking_level).argv
        for ordinal in range(int(os.environ.get("PROBE_COUNT", "3"))):
            persistent=backend.PersistentPiSession();startup=NativeStartupAdmission(c.root)
            launch=await asyncio.to_thread(NativePiRpcLaunch.managed,COMMAND,arguments,worktree=Path(original.worktree),environment=env,session_file=fork.session_file)
            preparation=ObservedPreparation(launch,'',session_file=fork.session_file,persistent_session=persistent,startup=startup)
            preparation.loop_pause=float(os.environ.get("PROBE_LOOP_PAUSE", "0"))
            begin=preparation.begin=time.monotonic();preparation.marks=[];samples=[];done=asyncio.Event()
            probe=asyncio.create_task(observe_loop(done,samples,begin,preparation))
            result={'ordinal':ordinal,'passed':False}
            try:
                async with asyncio.timeout(45),session_writer_fence(fork.session_file),persistent.lock:
                    async with aclosing(preparation.run()) as stream:
                        async for event in stream:
                            result.setdefault('events',[]).append(type(event).__name__)
                state=preparation.native.attestation.state
                assert state is not None and state.identity.session_file==fork.session_file
                result.update(passed=True,streaming=state.is_streaming,pending_messages=state.pending_message_count)
            except Exception as error:
                result['error']=f'{type(error).__name__}: {error}'
                result['diagnostic']=preparation.output.diagnostic
            finally:
                done.set();await probe
                startup.release();await persistent.close()
                result["native_stderr"]=await preparation.native.stderr_task
                backend.TurnSession.active.pop(asyncio.current_task(),None)
                result.update(elapsed=round(time.monotonic()-begin,3),marks=preparation.marks,samples=samples)
                receipt['attempts'].append(result)
                (EVIDENCE/(SUFFIX+'.json')).write_text(json.dumps(receipt,indent=2)+'\n')
                print(json.dumps({k:v for k,v in result.items() if k not in ('samples',)}),flush=True)
    after=Path(original.session_file).stat()
    receipt['original_history_stat_unchanged']=(before.st_size,before.st_mtime_ns)==(after.st_size,after.st_mtime_ns)
    (EVIDENCE/(SUFFIX+'.json')).write_text(json.dumps(receipt,indent=2)+'\n')

if __name__=='__main__':asyncio.run(main())
