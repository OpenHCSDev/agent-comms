"""Authentic000 provider-free original workers, distinct settings and carriers."""
import asyncio
from contextlib import suppress
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from agent_comms.active_route import ActiveRoute, publish_active_route
from agent_comms.activity import StoppedDrainDiagnostic
from agent_comms.comms import Comms
from agent_comms.coordination_cohort import accept_delivery_cohort
from agent_comms.coordinator import Coordination
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_states import BlockedGoal
from agent_comms.goals import Goal
from agent_comms.input_disposition import InputDispositions
from agent_comms.native_pi import NativePiUnavailable
from agent_comms.native_prompt_binding import read_expected_prompt_binding
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.owner_lifecycle import OwnerReleaseReceipt
from agent_comms.private_send_stage import TriageNativeSend
from agent_comms.runtime import socket_path
from agent_comms.runtime_requests import SubscribeRuntimeRequest
from agent_comms.selected_participant import SelectedParticipant
from agent_comms.selected_request import SelectedRequest
from agent_comms.selected_session import SelectedSession
from agent_comms.selected_turn import SelectedPrompt
from agent_comms.thread_identity import TurnId
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.threads import Thread
from agent_comms.turn_lease import ActiveTurn
from thread_format_retirement import GoalReportMemberRetirement


def historical_reservation(root, input_id):
    """Compare original declarations, not mutable SQLite file bytes."""
    with Coordination(str(root / 'coordination.sqlite3')) as store:
        with store.session.read():
            rows = NativeRuntimeInput.select(store.session._connection)
        assert len(rows) == 1 and rows[0].input_id == input_id
        assignment = store.assignments.get(rows[0].assignment_id)
        assert assignment.lifecycle.declared_name == 'deferred'
        original = [FieldCodec.encode(rows[0]), FieldCodec.encode(assignment),
                    FieldCodec.encode(read_expected_prompt_binding(store, input_id))]
    return hashlib.sha256(json.dumps(original, sort_keys=True).encode()).hexdigest()


def seed_historical_failure(service, root_id):
    """Author saved UNKNOWN using the authentic reservation/failure producers.

    No native execute/send method is replaced or invoked. This reproduces the
    old generic failure after reservation, without inventing a fresh unwritten
    prompt witness or retroactively applying the new NotSent classification.
    """
    owner = service.owners.acquire_thread('phase-alpha', owner_pid=os.getpid())
    service.registry.register(Thread('fixture-sender', frozenset(), str(service.root)),
                              StoppedThreadStatus(), new_owner=True)
    message = service.messaging.send_initial_cohort(
        'fixture-sender', '#phase-alpha', 'Protected historical channel input; never replay')
    initial = service.bus.log.read_delivery_cohort(root_id, message.seq)
    with Coordination(str(service.root / 'coordination.sqlite3')) as store:
        for recipient in initial.audience.recipients:
            store.participants.register(recipient.recipient_lookup, recipient.canonical_thread,
                                        recipient.canonical_thread, committed=True)
        accept_delivery_cohort(service.bus, root_id, message.seq, store)
        with SelectedParticipant.select(service, store, root_id, owner.name, 0) as participant:
            assert participant is not None
            prompt = SelectedPrompt(participant).triage()
            request = SelectedRequest.reserve(participant, SelectedSession(service.root/'native-sessions'),
                TriageNativeSend(participant.batch.assignments), 'historical-original-token', prompt)
            inputs = InputDispositions(service.root / InputDispositions.filename)
            key = inputs.bus_key(message, participant.owner.thread)
            admission = participant.owner.admission_generation
            assert inputs.record(key, seq=message.seq, owner=owner.name, admission=admission,
                                 target=message.target, text=message.body)
            assert inputs.bind(key, admission=admission,
                turn_id=participant.owner.thread.active_turn.id,
                native_id=request.admission.input_id, text=prompt)
            with suppress(NativePiUnavailable), request.native_failures():
                raise NativePiUnavailable('Historical native startup failure; disposition UNKNOWN')
            input_id = request.admission.input_id
    snapshot = service.registry.snapshot()
    identity = snapshot.owner_identity(owner.name)
    service.agents.set_drain_diagnostic(owner.name, identity,
        StoppedDrainDiagnostic(identity, 'NativePiUnavailable', 'Historical UNKNOWN; no retry'))
    os.environ['AGENT_COMMS_THREAD'] = owner.name
    service.owners.stop(owner.name)
    os.environ.pop('AGENT_COMMS_THREAD')
    return input_id


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
    service = Comms(root, private_initial_writes=True)
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
                        model='fixture-local/never-send',
                        task='Provider-free private cutover; never resume original work',
                        goal=Goal('Protected failed original', name+'-goal', state=BlockedGoal('No replay')))
        service.registry.declare(thread)
    historical_source = subprocess.run(
        [sys.executable, __file__, '--historical', str(root), root_id],
        text=True, capture_output=True, check=True)
    input_id = json.loads(historical_source.stdout)['input_id']
    historical = historical_reservation(root, input_id)
    for name, arguments, credential in settings:
        os.environ.update(BATCH_OWNER_CREDENTIAL=credential, PI_CODING_AGENT_DIR=str(root/name))
        service.owners.start(name, agent_args=arguments)
        asyncio.run(ready(root, service.registry.require(name)))
    service.registry.rename('phase-beta', 'phase-renamed')
    retired = Thread('phase-retired', frozenset(), str(root), last_goal_report_turn='old-retired-report')
    service.registry.register(retired, StoppedThreadStatus(), new_owner=True)
    service.owners.releases.update(lambda rows: {**rows, retired.name: OwnerReleaseReceipt(1, 2, retired)})
    InputDispositions(root / InputDispositions.filename).reserve_turn(
        'phase-alpha', TurnId('c7cc88d777b947d99b6f28f9e0b6ef97'), 1, 'Protected UNKNOWN; never replay')
    (root/'protected-native.jsonl').write_text(json.dumps({'type':'session','version':3,
        'id':'01a0d295-dfad-71f3-89f7-8a78d8917a73','timestamp':'2026-09-30T00:00:00Z',
        'cwd':str(root)})+'\n')
    source = service.registry.require('phase-renamed')
    service.registry.register(replace(source, active_turn=ActiveTurn('protected-busy', source.pid)))
    assert historical_reservation(root, input_id) == historical
    print(json.dumps({'root_id': root_id, 'historical_input_id': input_id,
        'historical_reservation_sha256': historical, 'owners': [FieldCodec.encode(
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
    if sys.argv[1] == '--historical':
        service = Comms(Path(sys.argv[2]), private_initial_writes=True)
        print(json.dumps({'input_id': seed_historical_failure(service, sys.argv[3])}))
    else:
        main()
