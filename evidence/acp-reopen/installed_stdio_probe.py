"""Real installed ACP stdio, existing owner, fresh input and native assistant receipt."""
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from acp import Client, spawn_agent_process
from acp.schema import TextContentBlock
from agent_comms.comms import wire

async def main():
    evidence = Path(__file__).resolve().parent
    comms = wire()
    owner = comms.registry.require('agent-comms-ux')
    assert owner.active_turn is None and owner.goal is None
    native = Path(owner.session_file)
    offset = native.stat().st_size
    updates = []
    marker = 'ACP_STDIO_PATH_OK'
    class Capture(Client):
        async def session_update(self, session_id, update, **kwargs):
            updates.append(update.model_dump(by_alias=True, exclude_none=True))
    environment = dict(os.environ)
    for key in ('PYTHONPATH','AGENT_COMMS_ROOT','AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID','AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE','AGENT_COMMS_THREAD','AGENT_COMMS_MANAGED','PI_AGENT_ID','PI_PARENT_ID','PI_TASK','PI_WORKTREE','PI_PROMPT'):
        environment.pop(key, None)
    environment['PATH'] = '/usr/local/bin:/usr/bin'
    report = {'verified': False, 'transport': 'installed ACP subprocess stdio', 'replayed_inputs': 0, 'path': environment['PATH']}
    started = time.monotonic()
    try:
        async with spawn_agent_process(Capture(), sys.executable, '-m', 'agent_comms.acp', env=environment, cwd=owner.worktree) as (connection, process):
            await connection.initialize(protocol_version=1)
            await connection.load_session(cwd=owner.worktree, session_id=owner.name, mcp_servers=[])
            updates.clear()
            response = await asyncio.wait_for(connection.prompt(session_id=owner.name, prompt=[TextContentBlock(type='text',text=f'Fresh installed ACP stdio verification. Reply exactly {marker}. Do not use tools, edit files, or resume prior work.')]),180)
            text = ''.join(u['content'].get('text','') for u in updates if u.get('sessionUpdate')=='agent_message_chunk' and u.get('content',{}).get('type')=='text')
            with native.open('rb') as stream:
                stream.seek(offset)
                rows = [json.loads(line) for line in stream if line.strip()]
            assistant = [r['message'] for r in rows if r.get('type')=='message' and r.get('message',{}).get('role')=='assistant']
            native_reply = any(''.join(c.get('text','') for c in m.get('content',[]) if c.get('type')=='text').strip()==marker and m.get('stopReason')=='stop' for m in assistant)
            report.update(assistant_text=text, native_assistant_verified=native_reply, response=response.model_dump(by_alias=True), same_owner_pid=comms.registry.require(owner.name).pid==owner.pid, update_count=len(updates))
            assert text.strip()==marker and native_reply and report['same_owner_pid'], report
            report['verified']=True
    finally:
        report['elapsed_seconds']=time.monotonic()-started
        (evidence/'installed-stdio-probe.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report,indent=2),flush=True)

asyncio.run(main())
