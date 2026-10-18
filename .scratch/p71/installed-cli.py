"""One fresh installed-CLI DM; preserve the wire and every native attempt."""
import asyncio
import json
import os
from pathlib import Path
import sys

import agent_comms
from agent_comms.comms import Comms
from agent_comms.coordinator import Coordination
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.field_codec import FieldCodec
from agent_comms.native_input_record import FullNativeExecution
from agent_comms.messages import Message
from agent_comms.native_package import verify_native_package
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.threads import Thread
from acp import spawn_agent_process
from acp.exceptions import RequestError

assert Path(agent_comms.__file__).resolve().is_relative_to(Path(sys.prefix)), agent_comms.__file__
sys.path.append('/home/ts/wt/core-doctrine-segment/tests')
from compaction_loopback import LoopbackProvider
from explicit_private_owner_installed_journey import Subscriber


async def main(stage, package):
    stage.mkdir(mode=0o700, parents=True, exist_ok=False)
    verify_native_package(package)
    provider = LoopbackProvider(status=200, text='P71_INSTALLED_CLI_REPLY')
    server = await asyncio.start_server(provider.handle, '127.0.0.1', 0)
    profile, project = stage / 'profile', stage / 'project'
    profile.mkdir(mode=0o700)
    project.mkdir(mode=0o700)
    (profile / 'models.json').write_text(json.dumps({'providers': {'selected-offline': {
        'baseUrl': f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1',
        'apiKey': 'local-fixture', 'api': 'openai-completions',
        'models': [{'id': 'fixture', 'contextWindow': 32768, 'maxTokens': 2048,
                    'compat': {'supportsUsageInStreaming': False}}],
    }}}))
    (profile / 'auth.json').write_text('{}')
    (profile / 'settings.json').write_text(json.dumps({'retry': {'enabled': False, 'maxRetries': 0}}))
    service = Comms(stage / 'w')
    root_id = service.messaging.initialize_private_initial_protocol()
    service.owners.pin_private_nk_launch(service.root, root_id, package)
    for name in ('sender', 'recipient'):
        service.registry.declare(Thread(name, frozenset(), str(project),
            model='selected-offline/fixture', thinking_level='off'))
    binary = Path(sys.executable).with_name('pi-comms-native')
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('PI_', 'AGENT_COMMS_', 'XDG_')) and key != 'PYTHONPATH'}
    env.update(AGENT_COMMS_ROOT=str(service.root), AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=root_id,
        AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(package), PI_CODING_AGENT_DIR=str(profile),
        AGENT_COMMS_NATIVE_CONFIG_DIR=str(profile), AGENT_COMMS_AGENT_BIN=str(binary),
        AGENT_COMMS_RUNTIME_ROOT=str(binary.parent), VIRTUAL_ENV=str(binary.parent.parent),
        PATH=str(binary.parent) + os.pathsep + env.get('PATH', ''),
        AGENT_COMMS_AGENT_MODELS='selected-offline/fixture',
        AGENT_COMMS_AGENT_ARGS='--offline --no-extensions --no-skills --no-context-files --no-tools',
        XDG_CONFIG_HOME=str(stage/'config'), XDG_DATA_HOME=str(stage/'data'), XDG_STATE_HOME=str(stage/'state'))
    number = 0

    async def cli(*arguments):
        nonlocal number
        number += 1
        child = await asyncio.create_subprocess_exec(str(binary.with_name('agent-comms')),
            '--root', str(service.root), *arguments, cwd=project, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(child.communicate(), 25)
        (stage / f'cli-{number}.json').write_bytes(stdout)
        (stage / f'cli-{number}.stderr').write_bytes(stderr)
        assert child.returncode == 0, (arguments, child.returncode, stdout, stderr)
        return json.loads(stdout)

    try:
        started = await cli('start', '--name', 'recipient')
        assert started['launched']
        subscriber = Subscriber()
        async with spawn_agent_process(subscriber, sys.executable, '-m', 'agent_comms.acp',
                                       env=env, cwd=project) as (connection, process):
            async with asyncio.timeout(30):
                await connection.initialize(protocol_version=1)
                try:
                    await connection.load_session(cwd=str(project), session_id='recipient', mcp_servers=[])
                except RequestError as error:
                    (stage/'load-error.json').write_text(json.dumps(error.to_error_obj(), indent=2))
                    raise
            sent = await cli('send', '--from', 'sender', '--to', 'recipient', '--body', 'P71_INSTALLED_CLI_DM')
            original = service.bus.log.message_by_id(sent['id'])
            async with asyncio.timeout(35):
                while True:
                    with Coordination(str(service.root/'coordination.sqlite3')) as store:
                        with store.session.read():
                            claims = WakeAssignment.select(store.session._connection,
                                where='wire_seq=?', parameters=(original.seq,))
                            inputs = NativeRuntimeInput.select(store.session._connection)
                    if len(claims) == 1 and claims[0].lifecycle.completed:
                        break
                    if claims and claims[0].lifecycle.terminal:
                        raise AssertionError(FieldCodec.encode(claims[0]))
                    await asyncio.sleep(.05)
            assert len(inputs) == 1 and inputs[0].reference_stage is FullNativeExecution
            inputs[0].reference.require_recorded()
            assert provider.posts == 1
            history = await cli('history', '--with', 'recipient', '--as', 'sender')
            replies = [message for row in history['dm']
                       if (message := Message.from_wire(row)).sender == 'recipient']
            assert len(replies) == 1 and replies[0].body.strip() == provider.text, history
            reader_inbox = await cli('inbox', '--thread', 'recipient')
            owner = service.registry.require('recipient')
            rows = map(json.loads, Path(owner.session_file).read_text().splitlines())
            users = [row['message'] for row in rows
                     if row.get('type') == 'message' and row['message'].get('role') == 'user']
            assert len(users) == 1 and 'P71_INSTALLED_CLI_DM' in str(users[0])
            receipt = {'installed_core': agent_comms.__file__, 'installed_python': sys.executable,
                'native_package': str(package), 'provider_requests': provider.posts,
                'native_users': len(users), 'assignments': FieldCodec.encode(claims),
                'native_inputs': FieldCodec.encode(inputs), 'reply': replies[0].to_wire(),
                'reader_inbox': reader_inbox, 'state': 'PASS'}
            (stage/'receipt.json').write_text(json.dumps(receipt, indent=2))
            print(json.dumps({'state': 'PASS', 'provider_requests': provider.posts,
                              'native_users': len(users), 'native_inputs': len(inputs), 'replies': len(replies)}))
    finally:
        if not service.registry.require('recipient').turn_state.busy:
            await cli('stop', '--name', 'recipient')
        server.close()
        await server.wait_closed()


asyncio.run(main(Path(sys.argv[1]).absolute(), Path(sys.argv[2]).absolute()))
