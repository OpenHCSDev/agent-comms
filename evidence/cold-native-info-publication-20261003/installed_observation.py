"""One installed configured SDK fork: cold and retained observation, no prompt."""

import asyncio
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import time
import tomllib
import zipfile
from pathlib import Path

import agent_comms
from agent_comms.acp import CommsAgent
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.comms import Comms
from agent_comms.coordinator import Coordination
from agent_comms.field_codec import FieldCodec
from agent_comms.input_disposition import InputDispositions
from agent_comms.native_fork import ForkSessionRequest
from agent_comms.native_package import MANIFEST, verify_native_package
from agent_comms.native_turn_context import NativeContextData
from agent_comms.owner_launch import RetainedOwnerLaunch
from agent_comms.runtime import RuntimeConnection, socket_path
from agent_comms.threads import Thread

checkout, stage, package = (Path(value).resolve() for value in sys.argv[1:4])
sys.path.insert(0, str(checkout / 'tests'))
from explicit_private_owner_installed_journey import Subscriber


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def installed_proof():
    installed = Path(agent_comms.__file__).parent
    assert installed.is_relative_to(Path(sys.prefix))
    names = subprocess.check_output(['git', 'ls-files', 'src/agent_comms'], cwd=checkout,
                                    text=True).splitlines()
    declared = {name.removeprefix('src/'): checkout / name for name in names}
    build = tomllib.loads((checkout / 'pyproject.toml').read_text())
    for source, target in build['tool']['hatch']['build']['targets']['wheel']['force-include'].items():
        assert target not in declared
        declared[target] = checkout / source
    wheel = checkout / '.artifacts/cold-native606-wheels/agent_comms-0.1.0-py3-none-any.whl'
    with zipfile.ZipFile(wheel) as bundle:
        members = {name for name in bundle.namelist() if name.startswith('agent_comms/') and not name.endswith('/')}
        assert members == declared.keys()
        for name, source in declared.items():
            assert source.read_bytes() == bundle.read(name) == (installed.parent / name).read_bytes(), name
    return {'python': sys.executable, 'core_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=checkout, text=True).strip(),
            'package_files': len(declared), 'python_files': sum(name.endswith('.py') for name in declared),
            'wheel_sha256': digest(wheel), 'native_manifest_sha256': digest(MANIFEST),
            'native_package': str(package), 'direct_url': json.loads(importlib.metadata.distribution('agent-comms').read_text('direct_url.json'))}


async def main():
    assert not stage.exists()
    stage.mkdir(mode=0o700)
    started = time.monotonic()
    receipt = {'state': 'PREPARING', 'proof': installed_proof(), 'root': str(stage),
               'public_inputs': 0, 'native_prompts': 0, 'provider_calls': 0, 'replays': 0}
    owner = None
    child = None
    originals = {}
    try:
        await Coordination.run_worker(lambda: verify_native_package(package))
        # Reuse the accepted original configured SDK source as a donor, never
        # modify its private wire, session, original input proofs or settings.
        donor = checkout / '.artifacts/cold-context595-configured02'
        evidence = json.loads((donor / 'terminal-receipt.json').read_text())
        source = Path(evidence['fork_file'])
        original = Comms(donor / 'wire').registry.require('configured-source')
        assert source == Path(original.require_saved_session())
        assert digest(source) == evidence['fork_sha256']
        public = Comms(Path('/var/tmp/agent-comms-live-20260927-wzjtqhza'))
        snapshot = await Coordination.run_worker(public.registry.snapshot)
        live = snapshot.require_active(evidence['original_name'])
        launch = await Coordination.run_worker(lambda: RetainedOwnerLaunch.capture(live, snapshot))
        assert live.model == original.model and live.thinking_level == original.thinking_level
        protected = [source, Path(evidence['source_file']), donor / 'terminal-receipt.json']
        protected += [path for path in (source.with_suffix(source.suffix + '.input-proof'),
                                        Path(evidence['source_file'] + '.input-proof')) if path.exists()]
        originals = {str(path): digest(path) for path in protected}
        service = Comms(stage / 'wire')
        fork = await CompactionJournal(service.root / 'compaction-commits.sqlite3').private_inputs.fork(
            ForkSessionRequest(str(package), str(source), original.worktree, str(stage / 'forks')),
            cwd=Path(original.worktree), env=dict(launch.environment))
        root_id = service.messaging.initialize_private_initial_protocol()
        service.owners.pin_private_nk_launch(service.root, root_id, package)
        runtime = Path(sys.executable).parent
        environment = dict(launch.environment)
        environment.update(AGENT_COMMS_ROOT=str(service.root), AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=root_id,
                           AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(package), PI_COMPACTION_TEST_PACKAGE=str(package),
                           AGENT_COMMS_AGENT_BIN=str(runtime / 'pi-comms-native'), PATH=str(runtime) + os.pathsep + environment.get('PATH', os.defpath))
        for key in ('PYTHONPATH', 'PI_PROMPT', 'PI_PARENT_ID', 'PI_TASK', 'PI_AGENT_ID',
                    'AGENT_COMMS_THREAD', 'AGENT_COMMS_STARTUP_INPUT_KEY'):
            environment.pop(key, None)
        os.environ.clear()
        os.environ.update(environment)
        owner = CommsAgent(service, agent_bin=str(runtime / 'pi-comms-native'),
                           agent_args=list(launch.arguments or ()), auto_wake=False, runtime_enabled=True,
                           private_nk_native_package=package, private_nk_wire_root_id=root_id)
        subscriber = Subscriber()
        owner.on_connect(subscriber)
        declaration = Thread('cold606', frozenset(), original.worktree, session_file=fork.session_file,
                             model=original.model, thinking_level=original.thinking_level)
        service.registry.declare(declaration)
        service.threads.restore_stopped(service.registry.snapshot(), (declaration.name,))
        thread = service.owners.acquire_thread(declaration.name, owner_pid=os.getpid())
        await owner._runtime.start()
        await owner.load_session(thread.worktree, thread.name)
        assert thread.name not in owner.turns.persistent_backends
        assert service.agents.agent_info_of(thread.name) is None
        selected = Path(fork.session_file)
        before = digest(selected)
        connection = RuntimeConnection(service, thread.name, socket_path(service.root, os.getpid()))
        try:
            async with asyncio.timeout(90):
                first = FieldCodec.decode(NativeContextData, await connection.request('context'))
                child = owner.turns.persistent_backends[thread.name].custody.idle().child
                first_info = service.agents.agent_info_of(thread.name)
                assert first_info is not None and first_info.model == thread.model
                assert first_info.context_size is not None and first_info.context_size > 0
                assert digest(selected) == before
                # Original runtime projection absence, with the acquired native
                # child still alive. No source/input/journal is cleared.
                service.agents.runtime_info.remove(thread.name)
                assert service.agents.agent_info_of(thread.name) is None
                second = FieldCodec.decode(NativeContextData, await connection.request('context'))
                second_info = service.agents.agent_info_of(thread.name)
                assert second_info is not None and second_info.timestamp >= first_info.timestamp
                assert (second_info.model, second_info.context_used, second_info.context_size) == (first_info.model, first_info.context_used, first_info.context_size)
                assert owner.turns.persistent_backends[thread.name].custody.idle().child is child
                assert first.identity == second.identity and first.segments == second.segments
                assert digest(selected) == before
                usages = [update for update in subscriber.updates if update.get('sessionUpdate') == 'usage_update']
                if second_info.context_used is not None:
                    assert [(update['used'], update['size']) for update in usages][-2:] == [(first_info.context_used, first_info.context_size), (second_info.context_used, second_info.context_size)]
                else:
                    assert usages == []
                assert not InputDispositions(service.root / InputDispositions.filename).read().rows
                service.registry.require(thread.name).require_idle()
                receipt.update(state='SCOPED_CONFIGURED_COLD_RETAINED_NATIVE_INFO_PASS',
                               donor_source=str(source), donor_bytes=source.stat().st_size,
                               fork_source=str(selected), fork_sha256=before,
                               configured_model=thread.model, configured_thinking=thread.thinking_level.declared_name,
                               cold_info=FieldCodec.encode(first_info), retained_info=FieldCodec.encode(second_info),
                               same_child=True, native_process=FieldCodec.encode(child.proc.identity),
                               current_context_segments=len(second.segments), usage_update_count=len(usages),
                               original_hashes=originals)
        finally:
            await connection.close()
    except BaseException as error:
        receipt.update(state='FAILED_NO_REPLAY', error=repr(error))
        raise
    finally:
        if owner is not None:
            await owner.shutdown()
        if child is not None:
            receipt['native_child_retired'] = child.proc.retired
            receipt['native_child_exited'] = not child.proc.alive()
            assert child.proc.retired and not child.proc.alive()
        receipt['original_hashes_unchanged'] = all(digest(Path(path)) == value for path, value in originals.items())
        assert receipt['original_hashes_unchanged']
        receipt['elapsed_seconds'] = time.monotonic() - started
        (stage / 'terminal-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
        print(json.dumps({key: receipt[key] for key in ('state', 'elapsed_seconds', 'root')}), flush=True)


asyncio.run(main())
