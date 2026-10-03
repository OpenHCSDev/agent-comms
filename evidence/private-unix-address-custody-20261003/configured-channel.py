"""One configured channel journey through existing fork, owner and ACP tools."""
import asyncio
from importlib import metadata
import json
import os
from pathlib import Path
import sys

here = Path(__file__).resolve().parent
checkout = here.parents[1]
sys.path.insert(0, str(checkout / 'tests'))
sys.path.insert(0, str(checkout / 'tools/cutover'))
from compaction_source_successor_installed_journey import digest
from explicit_private_owner_installed_journey import Subscriber
from original_owner_capture import CurrentTypedCapture
from acp import spawn_agent_process
from agent_comms.child_process import Platform, ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.coordinator import Coordination
from agent_comms.field_codec import FieldCodec
from agent_comms.native_fork import ForkSessionRequest
from agent_comms.native_package import MANIFEST, verify_native_package
from agent_comms.pi_vocabulary import ThinkingLevel
from agent_comms.store_files import _store_lock
from agent_comms.threads import Thread

stage = Path('/home/ts/.cache/agent-scratch/private-unix-address576-configured-channel-20261003/source-owner-admission-at-a-valid-persistent-directory-without-kernel-path-length-assumptions')
public = Path('/var/tmp/agent-comms-live-20260927-wzjtqhza')
native = Path('/home/ts/wt/comms-task-aware-native-bundle-20261002/stack/.pi-native-915c78ab702bd8ea/node_modules/@earendil-works/pi-coding-agent')
names = ('openhcs-helper', 'openhcs-helper2', 'openhcs-pr159-viewer-bind-owner')
receipt = {'complete': False, 'public_inputs': 0, 'replays': 0,
           'scope': 'Configured SDK-fork/native/stdio ACP/channel/peer handling; no physical UI or controlled speed comparison',
           'source': '8cafad04', 'python': sys.executable, 'native_manifest': digest(MANIFEST),
           'sdk': metadata.version('agent-client-protocol'), 'owners': [], 'cleanup': []}


def save():
    (here / 'configured-channel-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')


def claims(comms):
    with Coordination(comms.root / 'coordination.sqlite3') as resource:
        with resource.session.read():
            return WakeAssignment.select(resource.session._connection)


async def main():
    assert not stage.exists(), 'An existing attempt must never be replayed'
    assert Path(sys.prefix) == checkout / '.artifacts/runtime-scoped-input534'
    verify_native_package(native)
    stage.mkdir(parents=True, mode=0o700)
    service = Comms(stage / 'wire')
    root_id = service.messaging.initialize_private_initial_protocol()
    service.owners.pin_private_nk_launch(service.root, root_id, native)
    journal = CompactionJournal(service.root / 'compaction-commits.sqlite3')
    reader = CurrentTypedCapture(public, Path('/home/ts/.local/bin/agent-comms').resolve().with_name('python'))
    originals = {}
    selected = []
    original_goals = {}
    owned = []
    native_identities = set()
    driver = ProcessIdentity.capture(os.getpid())
    receipt['driver'] = FieldCodec.encode(driver)
    try:
        total = 0
        for name in names:
            capture = reader.read(name)
            original = capture.require_current()
            original.require_idle()
            file = Path(original.require_saved_session())
            total += file.stat().st_size
            assert total < 160 * 1024**2, 'Keep total fork histories within the 256MiB allocation'
            for path in (file, Path(str(file) + '.input-proof'),
                         *map(Path, capture.retained.configuration.settings_paths(Path(original.worktree)))):
                if path.is_file():
                    originals[str(path)] = digest(path)
            original_goals[name] = FieldCodec.encode(original.goal)
            fork = await journal.private_inputs.fork(ForkSessionRequest(
                str(native), str(file), original.worktree, str(stage / 'forks')),
                cwd=Path(original.worktree), env=dict(capture.retained.environment))
            capture.require_current().require_idle()
            service.registry.declare(Thread(name, original.tags, original.worktree,
                task=original.task, model=original.model, thinking_level=original.thinking_level,
                session_file=fork.session_file))
            selected.append((capture, fork))
            receipt['owners'].append({'original': FieldCodec.encode(capture.selection),
                'name': name, 'model': original.model,
                'thinking': ThinkingLevel.optional_name(original.thinking_level),
                'cwd': original.worktree, 'source': str(file), 'source_bytes': file.stat().st_size,
                'fork': FieldCodec.encode(fork),
                'configuration': FieldCodec.encode(capture.retained.configuration)})
            save()
            print('FORK', name, original.model, ThinkingLevel.optional_name(original.thinking_level), flush=True)

        binary = str(Path(sys.executable).with_name('pi-comms-native'))
        for capture, _ in selected:
            with _store_lock(service._wire_lock_path):
                owner = service.owners._launch_owner_unlocked(
                    service.registry.require(capture.source.name), binary,
                    capture.retained.arguments, environment=capture.retained.environment)
            owned.append(owner)
            receipt['owners'][len(owned)-1]['private_process'] = FieldCodec.encode(owner.require_process())
            save()

        sender = service.registry.require(names[0])
        env = dict(selected[0][0].retained.environment)
        for key in ('PYTHONPATH', 'PI_PROMPT', 'PI_AGENT_ID', 'AGENT_COMMS_THREAD', 'PI_PARENT_ID'):
            env.pop(key, None)
        env.update(AGENT_COMMS_ROOT=str(service.root),
            AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=root_id,
            AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(native), AGENT_COMMS_AGENT_BIN=binary)
        observer = Subscriber()
        async with spawn_agent_process(observer, sys.executable, '-m', 'agent_comms.acp',
                                       env=env, cwd=sender.worktree) as (connection, proxy):
            receipt['acp_process'] = FieldCodec.encode(ProcessIdentity.capture(proxy.pid))
            save()
            async with asyncio.timeout(45):
                await connection.initialize(protocol_version=1)
                await connection.load_session(cwd=sender.worktree, session_id=sender.name, mcp_servers=[])
            # Same original production producer as the existing channel roundtrip.
            message = await Coordination.run_worker(lambda: service.messaging.send_message(
                sender.name, '#openhcs',
                'Fresh changed-address private acceptance: please reply once and tersely if you receive this. No tools or edits.'))
            receipt['message'] = message.to_wire()
            save()
            print('ONE_CHANNEL_INPUT', message.seq, message.message_id, flush=True)
            async with asyncio.timeout(240):
                while True:
                    history = await Coordination.run_worker(lambda: service.views.channel_history('#openhcs'))
                    rows = await Coordination.run_worker(lambda: claims(service))
                    if rows:
                        receipt['claims'] = FieldCodec.encode(tuple(rows))
                        save()
                    for file in (service.root / 'diagnostics').glob('*.requests.jsonl'):
                        for line in file.read_text().splitlines():
                            row = json.loads(line)
                            if 'native_process' in row:
                                native_identities.add((row['native_process']['pid'], row['native_process']['start_time']))
                    replies = [row for row in history if row.seq != message.seq]
                    if len(replies) == 2 and {row.sender for row in replies} == set(names[1:]) and len(rows) == 6 and all(row.lifecycle.terminal for row in rows):
                        receipt['history'] = [row.to_wire() for row in history]
                        receipt['reply_delays_seconds'] = {row.sender: row.timestamp-message.timestamp for row in replies}
                        break
                    errors = tuple((service.root / 'diagnostics').glob('*.json'))
                    assert not errors, 'Original diagnostic exists; preserve rather than retry'
                    assert len(history) <= 3, 'Unexpected follow-up; do not create a new input'
                    await asyncio.sleep(.25)
            receipt['acp_updates'] = len(observer.updates)
            receipt['complete'] = True
            save()
            print('ALL_ORIGINAL_REPLIES_AND_PEER_CLAIMS_SETTLED', receipt['reply_delays_seconds'], flush=True)
    except BaseException as error:
        receipt['error'] = {'type': type(error).__name__, 'detail': str(error)}
        save()
        raise
    finally:
        for owner in reversed(owned):
            await asyncio.to_thread(service.owners.stop, owner.name)
        processes = [owner.require_process() for owner in owned] + [ProcessIdentity(*p) for p in native_identities]
        if 'acp_process' in receipt:
            processes.append(FieldCodec.decode(ProcessIdentity, receipt['acp_process']))
        receipt['cleanup'] = [{'identity': FieldCodec.encode(p), 'alive': p.alive(),
            'group': [FieldCodec.encode(member) for member in Platform.current().group_members(p)]} for p in processes]
        receipt['protected'] = {'before': originals,
            'after': {path: digest(Path(path)) for path in originals}}
        receipt['original_goals_unchanged'] = all(FieldCodec.encode(reader.read(name).source.goal)==goal for name,goal in original_goals.items())
        receipt['protected_unchanged'] = receipt['protected']['before'] == receipt['protected']['after']
        save()
        assert receipt['protected_unchanged'] and receipt['original_goals_unchanged']
        assert not any(row['alive'] or row['group'] for row in receipt['cleanup'])


asyncio.run(main())
