"""Normal installed native wrapper; copied saved session; get_state only."""

import asyncio
import importlib.util
import json
import os
import signal
import sys
from pathlib import Path

from agent_comms.active_route import read_active_route
from agent_comms.native_package import verify_native_package


async def main():
    repo = Path(__file__).resolve().parents[2]
    evidence = Path(__file__).resolve().parent
    runtime = Path(sys.executable).parent
    package = Path('/var/tmp/agent-comms-pi-native-extensions-20260928/node_modules/@earendil-works/pi-coding-agent')
    route = read_active_route()
    verify_native_package(package)
    copied_session = repo / '.artifacts/acp-reopen/session.jsonl'
    assert copied_session.is_file() and copied_session.parent.stat().st_mode & 0o777 == 0o700
    environment = dict(item.decode().split("=", 1) for item in
                       Path(f"/proc/{sys.argv[1]}/environ").read_bytes().split(b"\0") if item)
    environment.pop('PYTHONPATH', None)
    environment.update(
        PI_OFFLINE='1',
        AGENT_COMMS_ROOT=str(route.root), AGENT_COMMS_MANAGED='1',
        AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=route.wire_root_id,
        AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(package),
        AGENT_COMMS_THREAD='agent-comms-ux', PI_WORKTREE='/home/ts/.agent-comms',
    )
    # Existing fixture restricts networking in the child; configured extensions
    # still load normally and the Comms CLI uses this installed runtime.
    spec = importlib.util.spec_from_file_location('network_fixture', repo / 'stack/test-native-import-rpc.py')
    isolation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(isolation)
    args = [str(runtime / 'pi-comms-native'), '--print', '--mode', 'rpc',
            '--session', str(copied_session), '--provider', 'openai-codex',
            '--model', 'gpt-6-sol', '--thinking', 'high']
    child = await asyncio.create_subprocess_exec(
        *args, cwd='/home/ts/.agent-comms', env=environment,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True,
        preexec_fn=isolation.network_denial(),
    )
    stderr = asyncio.create_task(child.stderr.read())
    reply = None
    try:
        child.stdin.write(b'{"type":"get_state","id":"installed-startup"}\n')
        await child.stdin.drain()
        async with asyncio.timeout(45):
            while line := await child.stdout.readline():
                message = json.loads(line)
                if message.get('id') == 'installed-startup':
                    reply = message
                    break
        assert reply and reply['success'], 'Native startup did not reach ready'
        assert reply['data']['nativeInputProofCapability'] == 'pi-native-input-v1-live-only'
    finally:
        if child.returncode is None:
            os.killpg(child.pid, signal.SIGTERM)
        await asyncio.wait_for(child.wait(), 5)
        errors = (await stderr).decode(errors='replace')
        (evidence / 'installed-startup-stderr.txt').write_text(errors)
    assert not errors.strip(), errors
    result = {'inherited_worker_path': environment['PATH'], 'normal_automatic_extensions': True, 'get_state': True,
              'runtime': str(runtime.parent), 'native_package': str(package),
              'native_capability': reply['data']['nativeInputProofCapability'],
              'model': reply['data']['model']['id'],
              'message_count': reply['data']['messageCount'],
              'saved_session_bytes': copied_session.stat().st_size,
              'provider_prompts': 0, 'network': 'kernel-denied', 'live_writes': 0}
    (evidence / 'installed-startup.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
