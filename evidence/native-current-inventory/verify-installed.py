"""Fresh installed native identity + current MCP inventory; no provider calls."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = Path('/home/ts/.local/share/agent-comms/native-current-689ce4b5d0592b9a/node_modules/@earendil-works/pi-coding-agent')

async def verify():
    subprocess.run(['python3', str(ROOT/'src/agent_comms/native_package.py'), str(PACKAGE)], check=True)
    with tempfile.TemporaryDirectory(dir=ROOT/'.artifacts', prefix='fresh-native-') as temporary:
        fixture=Path(temporary)
        home=fixture/'home';home.mkdir()
        agent=fixture/'agent';agent.mkdir()
        (agent/'models.json').write_text(json.dumps({'providers':{'fixture':{'baseUrl':'http://127.0.0.1:1/v1','api':'openai-completions','models':[{'id':'fixture','name':'Local fixture','contextWindow':32768,'maxTokens':2048}]}}}))
        session=fixture/'session.jsonl'
        session_id=str(uuid.uuid4())
        session.write_text(json.dumps(dict(type='session', version=3, id=session_id, timestamp='2026-09-28T00:00:00.000Z', cwd=str(fixture)))+'\n')
        with session.open('a') as stream:
            for row in [dict(type='model_change',id='model-fixture',parentId=None,provider='fixture',modelId='fixture'),dict(type='thinking_level_change',id='thinking-fixture',parentId='model-fixture',thinkingLevel='off')]:
                stream.write(json.dumps(dict(timestamp='2026-09-28T00:00:00.000Z',**row))+'\n')
        session.chmod(0o600)
        before=session.read_bytes()
        env={'PATH':'/usr/local/bin:/usr/bin','HOME':str(home),'PI_CODING_AGENT_DIR':str(agent), 'PI_OFFLINE':'1', 'NODE_DISABLE_COMPILE_CACHE':'1','NO_COLOR':'1'}
        node=['node','--no-global-search-paths','--import', str(PACKAGE/'dist/agent-comms-import-fence.mjs'), '--import', str(PACKAGE/'dist/agent-comms-project-bootstrap.mjs')]
        cli=await asyncio.create_subprocess_exec(*node,str(PACKAGE/'dist/cli.js'),'--mode','rpc','--provider','fixture','--model','fixture','--offline','--no-extensions','--no-skills','--no-prompt-templates','--no-context-files','--no-tools','--session',str(session),cwd=fixture,env=env,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        stderr_task=asyncio.create_task(cli.stderr.read())
        responses={}
        try:
            async with asyncio.timeout(20):
                for command in ('get_state','get_messages'):
                    cli.stdin.write((json.dumps({'type':command,'id':command})+'\n').encode())
                    await cli.stdin.drain()
                    while True:
                        raw=await cli.stdout.readline()
                        assert raw, f'CLI exited {cli.returncode}: {(await stderr_task).decode()}'
                        event=json.loads(raw)
                        assert event.get('type') not in ('input_committed','context_committed'), event
                        if event.get('type')=='response' and event.get('id')==command:
                            assert event['success'],event
                            responses[command]=event['data'];break
                state=responses['get_state']
                assert state['sessionId']==session_id,state
                assert state['sessionFile']==str(session),state
                assert state['nativeInputProofCapability']=='pi-native-input-v1-live-only',state
        finally:
            if cli.returncode is None: cli.terminate()
            await asyncio.wait_for(cli.wait(),5)
            errors=(await stderr_task).decode()
        assert session.read_bytes()==before
        command=[*node,str(PACKAGE/'agent-comms-extensions/pi-mcp-client/bin/pi-mcp.mjs'),'inventory','--json']
        result=subprocess.run(command,env=env,cwd=fixture,capture_output=True,text=True,timeout=15)
        assert result.returncode==0,result.stderr
        inventory=json.loads(result.stdout)
        assert inventory['version']==2 and 'compatibility' not in inventory,inventory
        assert inventory['declarations']=={'user':[],'project':[]},inventory
        receipt={'source':'c3e7252a','package':str(PACKAGE),'manifest_sha256':hashlib.sha256((ROOT/'stack/pi-native.sha256').read_bytes()).hexdigest(), 'cli_identity_matches':True,'native_input_capability':state['nativeInputProofCapability'],'saved_session_unchanged':True,'provider_requests':0,'inventory_version':inventory['version'],'inventory_has_compatibility':False,'inventory':inventory,'cli_stderr':errors,'inventory_stderr':result.stderr,'fresh_cli_retired':cli.returncode is not None}
        (ROOT/'evidence/native-current-inventory/installed-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
        print(json.dumps(receipt,indent=2))

if __name__=='__main__': asyncio.run(verify())
