"""Fresh installed ACP initialization/load for each live owner; no prompt sent."""
import asyncio
import json
import os
import sys
from pathlib import Path

from agent_comms.child_process import AttachedChild
from agent_comms.comms import wire
from agent_comms.pi_rpc import PiRpcChannel


async def main():
    comms = wire()
    rows = []
    for owner in comms.registry.snapshot().threads.values():
        if not owner.process_alive:
            continue
        env = dict(os.environ)
        for key in ('PYTHONPATH', 'PI_AGENT_ID', 'PI_PROMPT', 'AGENT_COMMS_THREAD',
                    'AGENT_COMMS_ROOT', 'AGENT_COMMS_MANAGED'):
            env.pop(key, None)
        child = await AttachedChild.start(
            (sys.executable, '-m', 'agent_comms.acp'), cwd=Path(owner.worktree), env=env)
        records = PiRpcChannel(child.stdout)
        errors = asyncio.create_task(child.stderr.read())
        updates = 0
        try:
            calls = (
                ('initialize', {'protocolVersion': 1, 'clientCapabilities': {}}),
                ('session/load', {'sessionId': owner.name, 'cwd': owner.worktree,
                                  'mcpServers': []}),
            )
            for request_id, (method, params) in enumerate(calls, 1):
                child.stdin.write((json.dumps({'jsonrpc': '2.0', 'id': request_id,
                                               'method': method, 'params': params}) + '\n').encode())
                await child.stdin.drain()
                async with asyncio.timeout(25):
                    while True:
                        line = await records.readline()
                        if not line:
                            raise RuntimeError(f'ACP ended before {method}: {owner.name}')
                        message = json.loads(line)
                        if message.get('id') == request_id:
                            if 'error' in message:
                                raise RuntimeError(message['error'])
                            break
                        updates += message.get('method') == 'session/update'
            rows.append({'name': owner.name, 'attached': True,
                         'updates': updates, 'prompts_sent': 0})
        finally:
            await child.stop()
            stderr = await errors
            if stderr:
                Path(__file__).with_name(owner.name + '-acp-stderr.log').write_bytes(stderr)
    Path(__file__).with_suffix('.json').write_text(json.dumps(rows, indent=2) + '\n')
    print(json.dumps(rows, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
