"""Bounded post-cutover checks through installed CLI and official ACP transport."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys

import acp
from agent_comms.active_route import read_active_route
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.session_load import ExistingSessionLoadPlan

MAIN = Path('/home/ts/code/projects/agent-comms')
EVIDENCE = Path('/home/ts/wt/comms-update-latest/upgrade-evidence')


class Client(acp.Client):
    def __init__(self):
        self.updates = 0

    async def session_update(self, session_id, update, **kwargs):
        self.updates += 1


async def attached(comms):
    owner = comms.registry.require('codex-agent-comms')
    binding = comms.registry.snapshot().owner_binding(owner.name)
    process = await asyncio.create_subprocess_exec(sys.executable,'-m','agent_comms.acp',
        stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    client = Client()
    connection = acp.connect_to_agent(client,process.stdin,process.stdout)
    try:
        async with asyncio.timeout(45):
            await connection.initialize(protocol_version=1)
            await connection.load_session(cwd=owner.worktree,session_id=owner.name,
                mcp_servers=[],_meta=ExistingSessionLoadPlan(binding).metadata())
        return {'owner':owner.name,'pid':owner.pid,'updates':client.updates,'state':'attached'}
    finally:
        await connection.close()
        if process.returncode is None:
            process.terminate()
        await process.wait()


def main():
    # Match the installed launcher's route selection, not the old controller's
    # inherited native package or thread identity.
    for key in ('AGENT_COMMS_ROOT','AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID',
                'AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE','AGENT_COMMS_THREAD',
                'AGENT_COMMS_MANAGED','PI_AGENT_ID','PI_PARENT_ID','PI_TASK','PI_PROMPT'):
        os.environ.pop(key,None)
    route=read_active_route();assert route is not None
    assert route.observe_root()==Path.home()/'.agent-comms'
    comms=wire(route)
    # Publish only entry-point links through the existing publication owner;
    # do not run its queueing main(), which would restart the same owners twice.
    publication=runpy.run_path(str(MAIN/'tools/publish_runtime.py'))['RuntimePublication'].installed()
    publication.publish(route.root)
    receipt=json.loads((EVIDENCE/'receipt.json').read_text())
    snapshot=comms.registry.snapshot()
    for result in receipt['restarted']:
        owner=snapshot.require(result['thread'])
        assert owner.pid==result['pid'] and owner.process_alive
    for old in (EVIDENCE/'original-root').rglob('*.jsonl'):
        live=route.root/old.relative_to(EVIDENCE/'original-root')
        before=old.read_bytes()
        with live.open('rb') as source:
            assert source.read(len(before))==before, f'Retained prefix changed: {live}'
    outcome={'root':str(route.root),'thread_count':len(snapshot.threads),
             'retained_jsonl_prefixes':'unchanged','attachment':asyncio.run(attached(comms))}
    (EVIDENCE/'verification.json').write_text(json.dumps(outcome,indent=2))
    print(json.dumps(outcome),flush=True)
    # The existing goal scheduler resumes this same author for physical UI
    # review and the user-facing result; no input or installation is replayed.


if __name__=='__main__':
    main()
