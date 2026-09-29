"""Installed CLI root selection -> detached owner -> stdio ACP -> native reply."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys

from acp import spawn_agent_process
from acp.schema import TextContentBlock
from agent_comms.comms import Comms
from agent_comms.native_package import verify_native_package
from agent_comms.private_nk_entrypoint import PACKAGE_ENV, ROOT_ID_ENV
from agent_comms.runtime import socket_path
from agent_comms.threads import Thread
from compaction_loopback import LoopbackProvider


class Subscriber:
    def __init__(self):
        self.updates = []

    def on_connect(self, connection):
        pass

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append(update.model_dump(by_alias=True, exclude_none=True))

    async def request_permission(self, **kwargs):
        raise AssertionError("No permission request belongs to this localhost-only journey")


async def cli(environment, stage, root_args, arguments):
    child = await asyncio.create_subprocess_exec(
        str(Path(sys.executable).with_name('agent-comms')), *root_args,
        'invoke', '--tool', 'comms_start', '--arguments', json.dumps(arguments),
        cwd=stage, env=environment, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(child.communicate(), 15)
    return child.returncode, json.loads(stdout), stderr.decode()


async def main(package, evidence):
    verify_native_package(package)
    evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    provider = LoopbackProvider(status=200, text='EXPLICIT_ROUTE_NATIVE_REPLY')
    server = await asyncio.start_server(provider.handle, '127.0.0.1', 0)
    profile = evidence / 'profile'
    profile.mkdir(mode=0o700)
    (profile / 'models.json').write_text(json.dumps({'providers': {'selected-offline': {
        'baseUrl': f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1',
        'apiKey': 'local-fixture', 'api': 'openai-completions',
        'models': [{'id': 'fixture', 'contextWindow': 32768, 'maxTokens': 2048,
                    'compat': {'supportsUsageInStreaming': False}}],
    }}}))
    (profile / 'auth.json').write_text('{}')
    (profile / 'settings.json').write_text(json.dumps({'retry': {'enabled': False, 'maxRetries': 0}}))
    results = []
    try:
        for mode in ('cli', 'environment'):
            stage = evidence / mode
            stage.mkdir(mode=0o700)
            project = stage / 'project'
            project.mkdir(mode=0o700)
            comms = Comms(stage / 'wire')
            root_id = comms.messaging.initialize_private_initial_protocol()
            comms.registry.declare(Thread('recipient', frozenset({'team'}), str(project),
                                          model='selected-offline/fixture', thinking_level='off'))
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(('PI_', 'AGENT_COMMS_')) and key != 'PYTHONPATH'}
            env.update({ROOT_ID_ENV: root_id, PACKAGE_ENV: str(package),
                        'PI_CODING_AGENT_DIR': str(profile), 'AGENT_COMMS_AGENT_BIN': 'pi',
                        'AGENT_COMMS_AGENT_MODELS': 'selected-offline/fixture',
                        'AGENT_COMMS_AGENT_ARGS': '--offline --no-extensions --no-skills --no-context-files --no-tools',
                        'XDG_CONFIG_HOME': str(stage / 'config'), 'XDG_DATA_HOME': str(stage / 'data'),
                        'XDG_STATE_HOME': str(stage / 'state')})
            # Relative explicit selection must survive the owner's project cwd.
            root_args = ['--root', 'wire'] if mode == 'cli' else []
            if mode == 'environment':
                env['AGENT_COMMS_ROOT'] = 'wire'
            baseline_requests = provider.posts
            try:
                denied = []
                for defect in ('missing', 'partial', 'root-id', 'package'):
                    invalid = dict(env)
                    if defect == 'missing':
                        invalid.pop(ROOT_ID_ENV)
                        invalid.pop(PACKAGE_ENV)
                    elif defect == 'partial':
                        invalid.pop(PACKAGE_ENV)
                    elif defect == 'root-id':
                        invalid[ROOT_ID_ENV] = 'f' * 32
                    else:
                        invalid[PACKAGE_ENV] = str(stage / 'unreviewed-package')
                    status, reply, stderr = await cli(invalid, stage, root_args, {'name': 'recipient'})
                    owner = comms.registry.require('recipient')
                    denied.append({'case': defect, 'exit': status, 'reply': reply})
                    (stage / 'denials.json').write_text(json.dumps(denied, indent=2))
                    assert status == 1 and 'error' in reply, (mode, defect, status, reply, stderr)
                    assert owner.process_identity is None, 'Invalid authority forked/reserved an owner'
                    assert not (comms.root / 'diagnostics').exists(), 'Invalid authority reached launch'
                    assert provider.posts == baseline_requests
                print(mode, 'INVALID_AUTHORITY_DENIED_BEFORE_FORK', flush=True)
                status, reply, stderr = await cli(env, stage, root_args, {'name': 'recipient'})
                assert status == 0 and reply['launched'], (reply, stderr)
                owner = comms.registry.require('recipient')
                assert owner.pid == reply['pid'] and owner.pid != os.getpid()
                async with asyncio.timeout(20):
                    while not socket_path(comms.root, owner.pid).exists():
                        assert owner.process_alive, 'CLI-launched owner died before ACP attachment'
                        await asyncio.sleep(.05)
                # ACP is another actual installed subprocess, not an in-process stub.
                attached_env = dict(env, AGENT_COMMS_ROOT=str(comms.root))
                subscriber = Subscriber()
                async with spawn_agent_process(subscriber, sys.executable, '-m', 'agent_comms.acp',
                                               env=attached_env, cwd=project) as (connection, process):
                    async with asyncio.timeout(30):
                        await connection.initialize(protocol_version=1)
                        await connection.load_session(cwd=str(project), session_id='recipient', mcp_servers=[])
                        assert comms.registry.require('recipient').pid == owner.pid
                        await connection.prompt('recipient', [TextContentBlock(type='text', text=f'ONE_NATIVE_INPUT_{mode}')])
                    assert any('EXPLICIT_ROUTE_NATIVE_REPLY' in str(update) for update in subscriber.updates)
                assert provider.posts == baseline_requests + 1, 'One input made extra provider requests'
                owner = comms.registry.require('recipient')
                records = [json.loads(line) for line in Path(owner.session_file).read_text().splitlines()]
                users = [row for row in records if row.get('type') == 'message' and row['message'].get('role') == 'user']
                assert len(users) == 1 and f'ONE_NATIVE_INPUT_{mode}' in str(users[0])
                results.append({'mode': mode, 'denied_before_fork': denied, 'native_users': len(users),
                                'provider_requests': provider.posts - baseline_requests, 'reply': provider.text})
                (evidence / 'receipt.json').write_text(json.dumps(results, indent=2))
                print(mode, 'REAL_CLI_OWNER_STDIO_ACP_ONE_NATIVE_REPLY', flush=True)
            finally:
                await asyncio.to_thread(comms.owners.stop, 'recipient')
                diagnostics = comms.root / 'diagnostics'
                if diagnostics.exists():
                    shutil.copytree(diagnostics, stage / 'saved-diagnostics')
                # This private wire belongs only to this bounded acceptance run.
                shutil.rmtree(comms.root)
    finally:
        server.close()
        await server.wait_closed()
    (evidence / 'acceptance-complete.txt').write_text('PASS\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    arguments = parser.parse_args()
    asyncio.run(main(arguments.package, arguments.evidence))
