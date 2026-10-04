"""One installed current-preview sidebar read; no prompt or recorded-reader controls."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
import time


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


async def client(args):
    # This is a fresh real application process, separate from the context producer.
    from toad.app import ToadApp
    from toad.agent_schema import AgentDefinition
    from toad.widgets.context_explorer import ContextExplorer
    from toad.widgets.side_bar import SideBar, SideBarCollapsible
    from textual.widgets import Tree

    awareness_on_import = 'agent_comms.context_segments.awareness' in sys.modules
    definition = AgentDefinition.decode({
        'identity': 'current-preview637', 'name': 'Current preview', 'short_name': 'context',
        'protocol': 'acp', 'run_command': {'*': shlex.join((sys.executable, '-m', 'agent_comms.acp'))},
    })
    app = ToadApp(agent_data=definition, project_dir=args.project,
                  agent_session_id='configured-source')
    async with app.run_test(size=(150, 55)) as pilot:
        async with asyncio.timeout(40):
            await app.selected_session.wait_content_ready()
            view = app.selected_session
            sidebar, = (bar for bar in view.query(SideBar) if bar.right)
            sidebar.collapsed = False
            sidebar.schedule_hydration()
            await sidebar.wait_content_ready()
            explorer = view.query_one(ContextExplorer)
            explorer.query_ancestor(SideBarCollapsible).collapsed = False
            explorer.action_refresh()
            while not explorer.state.contains_native(bool):
                await pilot.pause()
                await asyncio.sleep(.05)
            await pilot.pause()
            native = explorer.state.native
            assert native.contributors
            tree = explorer.query_one(Tree)
            assert tree.context_nodes
            assert app._exception is None
            Path(args.output, 'current-preview.svg').write_text(app.export_screenshot())
            result = {'actual_registered_acp_app': True, 'current_preview_decoded': True,
                'ordinary_sidebar_mounted': True, 'contributors': [s.declared_name for s in native.contributors],
                'native_segments': [s.declared_name for s in native.segments],
                'tree_labels': [node.label.plain for node in tree.context_nodes.values()],
                'awareness_loaded_by_ordinary_app_import': awareness_on_import,
                'discovery_module_acquired': 'agent_comms.context_segments.awareness' in sys.modules,
                'provider_calls': 0, 'native_inputs': 0, 'physical_capture': False}
            Path(args.output, 'app-receipt.json').write_text(json.dumps(result, indent=2)+'\n')


async def controller(args):
    from agent_comms.active_route import read_active_route
    from agent_comms.acp import CommsAgent
    from agent_comms.comms import Comms
    from agent_comms.field_codec import FieldCodec
    from agent_comms.input_disposition import InputDispositions
    from toad.core.context_inspection import ContextInspection
    sys.path.append(str(Path(args.core_checkout) / 'tools/cutover'))
    from original_owner_capture import CurrentTypedCapture
    from publish_retained_summary import CohortActivation, InstalledSourceProof

    prefix = Path(sys.prefix).resolve()
    candidate = FieldCodec.decode(CohortActivation, json.loads(Path(args.staging_receipt).read_text()))
    proof = FieldCodec.decode(InstalledSourceProof, json.loads(candidate.staging_receipt.read_text()))
    proof.require_activation(candidate)
    assert candidate.stage.resolve() == prefix
    output = Path(args.output).resolve()
    output.mkdir(exist_ok=False)
    service = Comms(Path(args.fixture) / 'wire')
    previous = service.registry.require('configured-source')
    assert not previous.require_process().alive()
    file = Path(previous.require_saved_session())
    before = digest(file)
    inputs = InputDispositions(service.root / InputDispositions.filename).read()
    manifests = ContextInspection.read(service, previous.name).manifests
    captured = CurrentTypedCapture(read_active_route().root, Path(args.original_python)).read(
        'openhcs-audit-merged-runtime')
    source = captured.require_current()
    public_file = Path(source.require_saved_session())
    public_before = digest(public_file)
    assert (source.model, source.thinking_level) == (previous.model, previous.thinking_level)
    with service.bus.log.locked():
        metadata = service.bus.log.read_metadata_unlocked()
    assert metadata.private
    environment = dict(captured.retained.environment)
    environment.update(AGENT_COMMS_ROOT=str(service.root),
        AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=metadata.root_id,
        AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(candidate.native_package),
        AGENT_COMMS_AGENT_BIN=str(prefix / 'bin/pi-comms-native'),
        AGENT_COMMS_RUNTIME_ROOT=str(prefix / 'bin'),
        PATH=str(prefix / 'bin') + os.pathsep + environment.get('PATH', os.defpath),
        XDG_CONFIG_HOME=str(Path(args.fixture) / 'config'),
        XDG_STATE_HOME=str(output / 'state'), XDG_DATA_HOME=str(output / 'data'))
    for key in ('PYTHONPATH', 'AGENT_COMMS_THREAD', 'AGENT_COMMS_STARTUP_INPUT_KEY',
                'PI_PROMPT', 'PI_PARENT_ID', 'PI_TASK', 'PI_AGENT_ID'):
        environment.pop(key, None)
    os.environ.clear()
    os.environ.update(environment)
    service.threads.restore_stopped(service.registry.snapshot(), (previous.name,))
    service.owners.pin_private_nk_launch(service.root, metadata.root_id, candidate.native_package)
    owner = CommsAgent(service, private_nk_native_package=candidate.native_package,
        private_nk_wire_root_id=metadata.root_id, agent_bin=str(prefix / 'bin/pi-comms-native'),
        agent_args=list(captured.retained.arguments or ()), auto_wake=False, runtime_enabled=True)
    started = time.monotonic()
    result = {'state': 'RUNNING_CURRENT_PREVIEW_ONLY', 'providers': 0, 'native_inputs': 0,
              'new_forks': 0, 'priming_calls': 0}
    children = []
    try:
        declared = service.owners.acquire_thread(previous.name, owner_pid=os.getpid())
        assert declared.incarnation == previous.incarnation
        await owner._runtime.start()
        await owner.sessions.bind_owned(declared, declared.name)
        result['controller'] = FieldCodec.encode(declared.require_process())
        with (output / 'app.log').open('w') as log:
            app = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).resolve()),
                '--client', '--output', str(output), '--project', declared.worktree,
                stdout=log, stderr=asyncio.subprocess.STDOUT)
            result['app_pid'] = app.pid
            assert await app.wait() == 0, 'Inspect original app.log; no replay'
        result['state'] = 'INSTALLED_CURRENT_PREVIEW_APP_PASS'
    except BaseException as error:
        result.update(state='FAILED_NO_REPLAY', error=repr(error))
        raise
    finally:
        for backend in owner.turns.persistent_backends.values():
            if backend.custody.retained:
                children.append(backend.custody.idle().child.proc)
        await owner.shutdown()
        result.update(saved_source_unchanged=digest(file)==before,
            public_source_unchanged=digest(public_file)==public_before,
            original_inputs_unchanged=InputDispositions(service.root / InputDispositions.filename).read()==inputs,
            original_manifests_unchanged=ContextInspection.read(service, previous.name).manifests==manifests,
            owned_native_children_closed=all(child.retired and not child.alive() for child in children),
            elapsed_seconds=time.monotonic()-started)
        Path(output, 'terminal-receipt.json').write_text(json.dumps(result, indent=2)+'\n')
        print(json.dumps(result), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client', action='store_true')
    parser.add_argument('--output', required=True)
    parser.add_argument('--project')
    parser.add_argument('--core-checkout')
    parser.add_argument('--fixture')
    parser.add_argument('--original-python')
    parser.add_argument('--staging-receipt')
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    asyncio.run(client(args) if args.client else controller(args))
