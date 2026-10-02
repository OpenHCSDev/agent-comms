"""One configured saved-helper fork and fresh USER channel request, never a retry."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import time

import acp
from agent_comms.comms import Comms, wire
from agent_comms.coordinator import Coordination
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.coordination_tables.publications import PublicationReceipts
from agent_comms.field_codec import FieldCodec
from agent_comms.historical_native_inputs import read_historical_native_inputs
from agent_comms.native_fork import ForkSessionHelper, ForkSessionRequest
from agent_comms.native_input_record import FullNativeExecution, TriageNativeExecution
from agent_comms.native_package import verify_native_package
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.owner_launch import RetainedOwnerLaunch
from agent_comms.presentation import MessageNotification
from agent_comms.selected_triage import FullSelectedTriage
from agent_comms.threads import Thread
from agent_comms.bus_publication import stable_thread_lookup
from shared_bus_restart_native import attach


async def run(stage, package):
    import agent_comms

    checkout = Path(__file__).resolve().parents[1]
    installed = Path(agent_comms.__file__).resolve().parent
    assert installed.is_relative_to(Path(sys.prefix)), 'Use the installed candidate package'
    assert installed != checkout / 'src/agent_comms', 'No source overlay acceptance'
    for original in (checkout / 'src/agent_comms').rglob('*'):
        if original.is_file() and '__pycache__' not in original.parts:
            assert (installed / original.relative_to(checkout / 'src/agent_comms')).read_bytes() == original.read_bytes()
    assert stage.is_relative_to(checkout)
    stage.mkdir(mode=0o700, exist_ok=False)
    verify_native_package(package)
    public = wire()
    snapshot = public.registry.snapshot()
    source = snapshot.require_active('openhcs-helper')
    launch = RetainedOwnerLaunch.capture(source, snapshot)
    source_file = Path(source.session_file)
    with source_file.open('rb') as stream:
        source_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
    environment = dict(launch.environment)
    config = launch.configuration.native_config
    settings_hashes = {}
    for name in ('auth.json', 'models.json', 'settings.json'):
        original = config / name
        if original.exists():
            settings_hashes[str(original)] = hashlib.sha256(original.read_bytes()).hexdigest()
    # Only the offline canonical fork helper's output directory differs. Workers
    # retain the actual original model/settings/auth/extension environment.
    fork = await ForkSessionHelper.run(
        ForkSessionRequest(str(package), str(source_file), source.worktree, str(stage / 'forks')),
        cwd=Path(source.worktree),
        env=environment,
    )
    assert Path(fork.session_file).is_relative_to(stage)
    service = Comms(stage / 'w')
    root_id = service.messaging.initialize_private_initial_protocol()
    service.owners.pin_private_nk_launch(service.root, root_id, package)
    binary = Path(sys.executable).with_name('pi-comms-native')
    environment.update(AGENT_COMMS_ROOT=str(service.root),
        AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=root_id,
        AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(package),
        AGENT_COMMS_AGENT_BIN=str(binary),
        AGENT_COMMS_RUNTIME_ROOT=str(binary.parent),
        PATH=str(binary.parent) + os.pathsep + environment.get('PATH', ''),
        VIRTUAL_ENV=str(binary.parent.parent),
        AGENT_COMMS_DEBUG_LOG=str(stage / 'acp.log'))
    for key in ('PI_PROMPT', 'PI_PARENT_ID', 'PI_TASK', 'PI_AGENT_ID',
                'AGENT_COMMS_THREAD', 'AGENT_COMMS_STARTUP_INPUT_KEY', 'PYTHONPATH'):
        environment.pop(key, None)
    os.environ.clear()
    os.environ.update(environment)
    child = Thread('reply505', source.tags, source.worktree, parent=source.name,
        task=source.task, session_file=fork.session_file, model=source.model,
        thinking_level=source.thinking_level, execution=source.execution)
    service.registry.declare(child)
    assert 'openhcs' in child.tags
    packets = []

    class Observation(acp.Client):
        async def session_update(self, session_id, update, **kwargs):
            packets.append({'time': time.time(), 'session': session_id,
                            'update': update.session_update})

    receipt = {'state': 'prepared-no-input', 'stage': str(stage),
        'installed_python': sys.executable, 'installed_core': str(installed),
        'source_owner': source.name, 'source_session': str(source_file),
        'source_session_sha256_observed': source_hash,
        'canonical_fork': FieldCodec.encode(fork), 'model': source.model,
        'thinking': source.thinking_level.declared_name, 'worktree': source.worktree,
        'original_task_sha256': hashlib.sha256((source.task or '').encode()).hexdigest(),
        'original_launch_arguments_sha256': hashlib.sha256(json.dumps(launch.arguments).encode()).hexdigest(),
        'settings_hashes': settings_hashes, 'public_inputs': 0,
        'original_326_327_replays': 0, 'originals': [], 'proof': {}}

    def persist(state):
        receipt['state'] = state
        (stage / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
        print(state, flush=True)

    persist('canonical-saved-fork-prepared-no-input')
    try:
        await asyncio.to_thread(service.owners.start, child.name,
                                agent_bin=str(binary), agent_args=launch.arguments)
        await attach(service, child.name)
        async with acp.spawn_agent_process(Observation(), sys.executable, '-m', 'agent_comms.acp',
                                           env=environment, cwd=source.worktree) as (client, process):
            await client.initialize(protocol_version=1)
            await client.load_session(cwd=source.worktree, session_id=child.name, mcp_servers=[])
            persist('ordinary-installed-acp-saved-load-ready-before-input')
            original = service.messaging.send_user_message('#openhcs',
                'Please reply tersely if you receive this. This is a distinct current '
                'connectivity request, not continuation of your assigned task or a retry. '
                'Reply with Received 505-20261002-a1.', worktree=source.worktree)
            receipt['originals'].append(FieldCodec.encode(original.reference))
            persist('one-distinct-user-channel-request-published')
            async with asyncio.timeout(300):
                while True:
                    with Coordination(str(service.root / 'coordination.sqlite3')) as store:
                        with store.session.read():
                            db = store.session._connection
                            claims = WakeAssignment.select(db, where='wire_seq=?', parameters=(original.seq,))
                            inputs = NativeRuntimeInput.select(db)
                            publications = PublicationReceipts.select(db)
                        if len(claims) == 1 and claims[0].lifecycle.completed:
                            triage = tuple(row for row in inputs if row.reference_stage is TriageNativeExecution)
                            full = tuple(row for row in inputs if row.reference_stage is FullNativeExecution)
                            assert len(triage) == len(full) == 1
                            assert triage[0].verdict is FullSelectedTriage
                            assert claims[0].lifecycle.mode.triage
                            replies = tuple(row for row in publications if row.execution_id == claims[0].lifecycle.execution_id)
                            reply, = replies
                            assert reply.exact_target == '#openhcs'
                            actual = service.bus.log.message_by_id(reply.message_id)
                            assert actual.sender == child.name and actual.body.strip()
                            history = read_historical_native_inputs(store, wire_root_id=root_id,
                                recipient_lookup=stable_thread_lookup(service.registry.require(child.name).created_at),
                                source_seq=original.seq)
                            assert len(history) == 2 and all(row.expected_prompt_equality_established for row in history)
                            handling = MessageNotification.window(service.root, service.registry,
                                service.bus.log, [original])[(original.seq, original.message_id)]
                            notification, = handling
                            assert notification.state == 'Responded'
                            receipt['proof'] = {'assignment': FieldCodec.encode(claims[0]),
                                'native_inputs': FieldCodec.encode(inputs),
                                'original_publication': FieldCodec.encode(reply),
                                'canonical_reply': FieldCodec.encode(actual),
                                'original_handling': FieldCodec.encode(notification),
                                'original_historical_source_proofs': 2}
                            persist('configured-saved-triage-full-canonical-channel-reply-handling-PASS')
                            break
                    if claims and claims[0].lifecycle.terminal:
                        receipt['terminal_assignment'] = FieldCodec.encode(claims[0])
                        raise AssertionError('Original request ended without a canonical reply')
                    diagnostics = tuple((service.root / 'diagnostics').glob('*.json'))
                    if diagnostics:
                        receipt['diagnostics'] = [str(path) for path in diagnostics]
                        raise RuntimeError('Actual native/owner diagnostic; preserve input, never retry')
                    await asyncio.sleep(.1)
    except BaseException as error:
        receipt['failure'] = f'{type(error).__name__}: {error}'
        persist('FAILED_OR_UNRESOLVED_NO_REPLAY')
        raise
    finally:
        current = service.registry.require(child.name)
        # Observation expiry cannot authorize aborting a still-running input.
        if current.active_turn is None:
            await asyncio.to_thread(service.owners.stop, child.name)
        receipt['owned_owner_after_observation'] = FieldCodec.encode(service.registry.require(child.name).process_identity)
        receipt['configured_source_hashes_unchanged'] = all(
            hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
            for path, digest in settings_hashes.items())
        receipt['acp_observations'] = packets
        (stage / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')


if __name__ == '__main__':
    asyncio.run(run(Path(sys.argv[1]).absolute(), Path(sys.argv[2]).absolute()))
