"""Authentic000 provider-free original workers, distinct settings and carriers."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sys

from agent_comms.active_route import ActiveRoute, publish_active_route
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_states import BlockedGoal
from agent_comms.goals import Goal
from agent_comms.input_disposition import InputDispositions
from agent_comms.owner_lifecycle import OwnerReleaseReceipt
from agent_comms.runtime import socket_path
from agent_comms.runtime_requests import SubscribeRuntimeRequest
from agent_comms.thread_identity import TurnId
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.threads import Thread
from agent_comms.turn_lease import ActiveTurn
from thread_format_retirement import GoalReportMemberRetirement


async def ready(root, owner):
    async with asyncio.timeout(12):
        while not socket_path(root, owner.pid).exists():
            assert owner.process_alive
            await asyncio.sleep(.02)
        reader, writer = await asyncio.open_unix_connection(socket_path(root, owner.pid), limit=8*1024*1024)
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


def main():
    root, route = map(Path, sys.argv[1:])
    root.mkdir(mode=0o700)
    route.parent.mkdir(mode=0o700)
    service = Comms(root)
    root_id = service.messaging.initialize_private_initial_protocol()
    package = Path(os.environ['AC_NATIVE_COPIED_PACKAGE'])
    service.owners.pin_private_nk_launch(root, root_id, package)
    os.environ.update(AGENT_COMMS_ROOT=str(root), AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=root_id,
                      AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(package))
    publish_active_route(ActiveRoute(root, root_id, package), route)
    settings = (('phase-alpha', ('--offline', '--no-tools', '--thinking', 'off'), 'fixture-alpha'),
                ('phase-beta', (), 'fixture-beta'))
    for name, arguments, credential in settings:
        thread = Thread(name, frozenset({name}), str(root),
                        last_goal_report_turn=name+'-retired-report',
                        task='Provider-free private cutover; never resume original work',
                        goal=Goal('Protected failed original', name+'-goal', state=BlockedGoal('No replay')))
        service.registry.declare(thread)
        os.environ.update(BATCH_OWNER_CREDENTIAL=credential, PI_CODING_AGENT_DIR=str(root/name))
        service.owners.start(name, agent_args=arguments)
        asyncio.run(ready(root, service.registry.require(name)))
    service.registry.rename('phase-beta', 'phase-renamed')
    retired = Thread('phase-retired', frozenset(), str(root), last_goal_report_turn='old-retired-report')
    service.registry.register(retired, StoppedThreadStatus(), new_owner=True)
    service.owners.releases.replace({retired.name: OwnerReleaseReceipt(1, 2, retired)})
    InputDispositions(root / InputDispositions.filename).reserve_turn(
        'phase-alpha', TurnId('c7cc88d777b947d99b6f28f9e0b6ef97'), 1, 'Protected UNKNOWN; never replay')
    (root/'protected-native.jsonl').write_text(json.dumps({'type':'session','version':3,
        'id':'01a0d295-dfad-71f3-89f7-8a78d8917a73','timestamp':'2026-09-30T00:00:00Z',
        'cwd':str(root)})+'\n')
    source = service.registry.require('phase-renamed')
    service.registry.register(replace(source, active_turn=ActiveTurn('protected-busy', source.pid)))
    print(json.dumps({'root_id': root_id, 'owners': [FieldCodec.encode(
        service.registry.require(name).process_identity) for name in ('phase-alpha','phase-renamed')],
        'original_threads': [GoalReportMemberRetirement.thread(FieldCodec.encode(
            service.registry.require(name))) for name in ('phase-alpha','phase-renamed')]}), flush=True)
    for command in sys.stdin:
        if command.strip() == 'idle':
            service.registry.register(source)
            print('idle', flush=True)
        else:
            break


if __name__ == '__main__':
    main()
