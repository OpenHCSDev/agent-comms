"""Report-authoring helper and current-runtime replacement fixture."""

from agent_comms.runtime_socket import RuntimeSocket
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sys

from agent_comms.comms import Comms
from agent_comms.goal_actions import BlockedGoalAction, ModelInvocable, SetGoalAction

from agent_comms.runtime_requests import SubscribeRuntimeRequest
from agent_comms.turn_lease import ActiveTurn
from agent_comms.threads import Thread
from agent_comms.active_route import LocalRoute
from agent_comms.native_settings import NativeThreadDefaults
from agent_comms.launch_runtime import LaunchRuntime


def author_model_report(service, name, turn):
    """Original actions and registry commits author the preserved report fact."""
    service.goals.update_goal(name, SetGoalAction(text='Private original report fixture'))
    source = service.registry.require(name)
    service.registry.register(replace(source, active_turn=ActiveTurn(turn, source.pid)))
    service.goals.update_goal(name, BlockedGoalAction(block_reason='No original work replay'),
                              actor=ModelInvocable)
    source = service.registry.require(name)
    service.registry.register(replace(source, active_turn=None))


async def ready(root, owner):
    async with asyncio.timeout(12):
        while not RuntimeSocket.path_for(root, owner.pid).exists():
            assert owner.process_alive
            await asyncio.sleep(.02)
        reader, writer = await asyncio.open_unix_connection(RuntimeSocket.path_for(root, owner.pid), limit=8*1024*1024)
        try:
            writer.write((json.dumps(SubscribeRuntimeRequest(thread=owner.name).to_wire())+'\n').encode())
            await writer.drain()
            while line := await reader.readline():
                packet = json.loads(line)
                assert 'error' not in packet, packet
                if 'ready' in packet:
                    return
            raise AssertionError('Original worker closed before attachment')
        finally:
            writer.close()
            await writer.wait_closed()


def runtime_owners(root, package):
    """Seed real idle source-runtime workers and SDK-authored saved history."""
    from agent_comms.fresh_private_session import create_fresh_private_session
    from agent_comms.goal_states import BlockedGoal
    from agent_comms.goals import Goal
    from native_backend_fixture import NativeBackendFixture

    root.mkdir(mode=0o700)
    service = Comms(LocalRoute(root), runtime=LaunchRuntime.current(), private_initial_writes=True)
    root_id = service.messaging.initialize_private_initial_protocol()
    service.owners.pin_private_nk_launch(root, root_id, package)
    os.environ.update(AGENT_COMMS_ROOT=str(root),
        AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=root_id,
        AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(package),
        PI_COMPACTION_TEST_PACKAGE=str(package))
    names = ('replacement-alpha', 'replacement-beta')
    try:
        for index, name in enumerate(names):
            project = root.parent / name
            project.mkdir(mode=0o700)
            saved = create_fresh_private_session(project / 'sessions', worktree=project)
            fixture = NativeBackendFixture(root, project, saved.path, None, project)
            asyncio.run(fixture.author_history())
            service.registry.declare(Thread(name, frozenset({name}), str(project),
                session_file=str(saved.path), model='response-local/fixture', thinking_level='off',
                task='Idle private replacement fixture',
                goal=Goal('Do not invoke provider', name+'-goal', state=BlockedGoal('No input'))))
            os.environ.update(PI_CODING_AGENT_DIR=str(project / 'config'),
                              BATCH_OWNER_CREDENTIAL='private-'+str(index))
            service.owners.start(name, agent_args=('--offline', '--no-tools') if index == 0 else ())
            asyncio.run(ready(root, service.registry.require(name)))
        print(json.dumps({'root_id':root_id, 'names':names}), flush=True)
        # The operator replaces workers while this source fixture retains cleanup.
        sys.stdin.readline()
    finally:
        for name in names:
            if name in service.registry.snapshot().threads:
                service.owners.stop(name)


if __name__ == '__main__':
    if sys.argv[1] != '--runtime-owners':
        raise ValueError('Select the current-runtime owner fixture')
    runtime_owners(Path(sys.argv[2]), Path(sys.argv[3]))
