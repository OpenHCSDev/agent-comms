"""Actual configured native launchers: fresh isolated sessions, no prompt/provider call."""
import asyncio,json,os,shlex
from pathlib import Path
from tempfile import TemporaryDirectory
from agent_comms.child_process import AttachedChild
from agent_comms.comms import wire
from agent_comms.fresh_private_session import create_fresh_private_session
from agent_comms.native_pi import CAPABILITY,NativePiRpcLaunch
from agent_comms.pi_commands import GetState
from agent_comms.pi_events import Response
from agent_comms.pi_rpc import PiRpcChannel

async def main():
    live=wire();rows=[]
    for owner in live.registry.snapshot().threads.values():
        if not owner.process_alive:continue
        fields=Path(f'/proc/{owner.pid}/environ').read_bytes().split(b'\0')
        config={k.decode():v.decode() for entry in fields if b'=' in entry for k,v in (entry.split(b'=',1),)}
        command=config['AGENT_COMMS_AGENT_BIN']
        arguments=tuple(shlex.split(config.get('AGENT_COMMS_AGENT_ARGS','')))
        # Validate the exact worker's command/pin in this same core installation.
        for key in ('AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID','AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE','AGENT_COMMS_ROOT'):
            os.environ[key]=config[key]
        with TemporaryDirectory(prefix='launcher-native-',dir=Path.cwd()/'.artifacts') as folder:
            scratch=Path(folder)
            session=create_fresh_private_session(scratch/'sessions',worktree=Path(owner.worktree)).path
            launch=NativePiRpcLaunch.managed(command,(*arguments,'--offline','--no-extensions','--no-skills','--no-context-files','--no-prompt-templates','--no-tools'),worktree=Path(owner.worktree),environment=config,session_file=str(session))
            child=await AttachedChild.start(launch.argv,cwd=launch.cwd,env=launch.env)
            errors=asyncio.create_task(child.stderr.read())
            reader=PiRpcChannel(child.stdout)
            request=GetState(id='deployment-launch-check')
            reader.pending.add(GetState,request.id,request=request)
            try:
                child.stdin.write(PiRpcChannel.command_bytes(request));await child.stdin.drain()
                async with asyncio.timeout(20):
                    while True:
                        raw=await reader.readline()
                        if not raw:raise RuntimeError('Native ended before GetState')
                        response=reader.decode_record(raw)
                        if isinstance(response,Response) and response.id==request.id:
                            assert response.success
                            assert response.data.native_input_proof_capability==CAPABILITY
                            break
                rows.append({'owner':owner.name,'configured_launcher':command,'actual_native_attestation':True,'prompts_sent':0})
            finally:
                await child.stop();await errors
    Path(__file__).with_suffix('.json').write_text(json.dumps(rows,indent=2)+'\n')
    print(json.dumps(rows,indent=2))

asyncio.run(main())
