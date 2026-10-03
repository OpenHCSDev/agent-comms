"""Installed original runtime/project IPC at a long root; no provider or input."""
import asyncio
from contextlib import ExitStack
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tests'))
from delivery_owner_fixture import canonical_agent
from agent_comms.comms import Comms
from agent_comms.child_process import ProcessIdentity
from agent_comms.native_custody import PiSessionChild
from agent_comms.native_attestation import PendingAttestation
from agent_comms.native_pi import NativePiRpcLaunch
from agent_comms.private_path import PrivateSocketRole
from agent_comms.runtime import RuntimeConnection, socket_path
from agent_comms.runtime_requests import ProjectRuntimeRequest

stage = Path('/home/ts/.cache/agent-scratch/m576-runtime-address-original-control') / ('persistent-root-' * 8)
assert not stage.exists()
stage.mkdir(parents=True, mode=0o700)
package = Path(os.environ['PI_COMPACTION_TEST_PACKAGE'])
receipt = {'scope':'Actual installed runtime IPC and PiSessionChild address-resource custody, Node project client; no SDK/provider/input claim', 'complete':False}
async def main():
    comms=Comms(stage/'wire')
    owner=canonical_agent(comms, agent_bin=str(Path(sys.executable).with_name('pi-comms-native')),agent_args=[],runtime_enabled=True)
    child=None
    try:
        session=(await owner.new_session(str(stage))).session_id
        thread=comms.registry.require(session)
        request=ProjectRuntimeRequest.for_native(comms.registry.snapshot(),thread)
        path=socket_path(comms.root,thread.pid)
        receipt['socket_bytes']=len(os.fsencode(path))
        connection=RuntimeConnection(comms,session,path)
        receipt['project'] = await connection.request('project', binding=request.to_wire()['binding'])
        await connection.close()
        environment=thread.native_environment(comms.root,comms.registry.snapshot(),str(stage))
        launch=NativePiRpcLaunch.managed(str(Path(sys.executable).with_name('pi-comms-native')), (), worktree=stage,environment=environment,session_file=None)
        node="""const net=require('node:net'); const fs=require('node:fs');
const path=process.env.AGENT_COMMS_PROJECT_SOCKET;
if(Buffer.byteLength(path)>107 || !fs.existsSync(path)) throw Error('invalid acquired address');
const s=net.createConnection(path); let data='';
s.on('connect',()=>s.write(process.env.AGENT_COMMS_PROJECT_REQUEST+'\\n'));
s.on('data',chunk=>{ data+=chunk; if(data.includes('\\n')) { console.log(data.trim());s.end(); }});
s.on('error',e=>{console.error(e);process.exitCode=1;});"""
        launch=replace(launch,argv=('node','-e',node))
        child=await PiSessionChild.start((launch,(0,0)),PendingAttestation())
        identity=ProcessIdentity.capture(child.proc.pid)
        record=await asyncio.wait_for(child.reader.readline(),10)
        reply=json.loads(record)
        assert reply['result']['worktree']==str(stage),reply
        receipt['native_resource_client']=reply
        await child.close()
        receipt['child_absent']=not identity.alive()
        receipt['child_resources_closed']=not child.resources._exit_callbacks
        assert receipt['child_absent'] and receipt['child_resources_closed']
        assert path.is_socket()
        receipt['complete']=True
    finally:
        if child is not None: await child.close()
        await owner.shutdown()
        receipt['socket_removed']=not socket_path(comms.root,os.getpid()).exists()
        Path(__file__).with_name('runtime-address-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
asyncio.run(main())
