"""Installed native resource inspection; no prompt, provider call or public write."""
import asyncio, hashlib, json, os, pathlib, time
from agent_comms.native_arguments import NativeArguments
from agent_comms.native_custody import PiSessionChild
from agent_comms.native_package import OWNER_INSTRUCTIONS
from agent_comms.native_pi import NativePiRpcLaunch, _private_agent_dir
from agent_comms.owner_launch import RetainedOwnerLaunch
from agent_comms.pi_commands import AgentCommsInspectContext, GetState
from agent_comms.pi_events import Response
from agent_comms.pi_vocabulary import ThinkingLevel
from agent_comms.registration import Registration
from agent_comms.selected_session import SelectedSession
from agent_comms.turn_context import FileProvenance, SystemLayerSegment

HERE = pathlib.Path(__file__).parent
PACKAGE = pathlib.Path('/home/ts/wt/comms-task-aware-native-bundle-20261002/stack/.pi-native-ad533a9f08581561/node_modules/@earendil-works/pi-coding-agent')
snapshot = Registration(pathlib.Path('/var/tmp/agent-comms-live-20260927-wzjtqhza/registry.json')).snapshot()
original = OWNER_INSTRUCTIONS.read_bytes()
receipt = {'source': 'installed normal Core531 wheel', 'native_package': str(PACKAGE),
           'instruction_file': str(OWNER_INSTRUCTIONS), 'instruction_sha256': hashlib.sha256(original).hexdigest(),
           'native_trust': 'original matching Core529 verifier passed before inspection',
           'prompt_commands': 0, 'provider_calls': 0, 'public_mutations': [], 'journeys': []}

async def inspect(name):
    thread = snapshot.require_active(name)
    captured = RetainedOwnerLaunch.capture(thread, snapshot)
    owned = HERE / ('launch03-' + name)
    owned.mkdir(mode=0o700)
    session = SelectedSession(owned / 'native')
    session.directory.mkdir(mode=0o700)
    configuration = captured.configuration.for_agent(_private_agent_dir(session.directory))
    environment = dict(captured.environment)
    for key in ('AGENT_COMMS_THREAD', 'PI_AGENT_ID', 'PI_PARENT_ID', 'AGENT_COMMS_MANAGED',
                'AGENT_COMMS_AGENT_BIN', 'AGENT_COMMS_AGENT_ARGS',
                'AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID', 'AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE'):
        environment.pop(key, None)
    environment['AGENT_COMMS_ROOT'] = str(owned / 'private-wire')
    arguments = NativeArguments.parse(('--mode', 'rpc', '--no-approve', '--no-tools',
                                       '--session-dir', str(session.directory))).with_model(thread.model).with_thinking(ThinkingLevel.optional_name(thread.thinking_level))
    launch = NativePiRpcLaunch._build(PACKAGE / 'dist/cli.js', arguments.argv,
                                      pathlib.Path(thread.worktree), environment, session, PACKAGE, configuration)
    child = await PiSessionChild.start((launch, configuration.auth_revision()), session.attestation())
    started = time.monotonic()
    result = {'configured_owner': name, 'cwd': str(launch.cwd), 'model': thread.model,
              'thinking': ThinkingLevel.optional_name(thread.thinking_level), 'cwd_append_exists': (launch.cwd / '.pi/APPEND_SYSTEM.md').is_file(),
              'append_cli_count': launch.argv.count('--append-system-prompt'), 'pid': child.proc.pid}
    receipt['journeys'].append(result)
    try:
        request = GetState(id='531-state')
        await child.proc.write(child.reader.encode(request))
        async with asyncio.timeout(60):
            while True:
                event = await child.reader.receive(strict=True)
                if isinstance(event, Response) and child.reader.correlate(event) is request:
                    state = event.require_request(request)
                    assert event.success and state.message_count == 0 and state.pending_message_count == 0
                    break
            response = await AgentCommsInspectContext().exchange(child.reader, child.proc.stdin)
        assert response.success
        context = response.data.require_payload()
        system = [s for s in context.segments if isinstance(s, SystemLayerSegment)]
        assert len(system) == 1
        files = [p for p in system[0].provenance if isinstance(p, FileProvenance)]
        canonical = [p for p in files if p.path == str(OWNER_INSTRUCTIONS.resolve())]
        cwd_append = [p for p in files if p.path == str(launch.cwd / '.pi/APPEND_SYSTEM.md')]
        assert len(canonical) == 1 and not cwd_append
        assert canonical[0].sha256 == receipt['instruction_sha256']
        assert system[0].content.count(original.decode()) == 1
        result.update(state='PASS', canonical_file_count=len(canonical), discovered_cwd_append_count=len(cwd_append),
                      full_canonical_content_count=1, system_layers=len(system),
                      loaded_file_sources=[{'path': p.path, 'sha256': p.sha256} for p in files],
                      commands=['get_state', 'agent_comms_inspect_context'])
    finally:
        child.reader.pending.cancel_all()
        await child.close()
        result['child_retired'] = not child.proc.identity.alive()
        result['stderr_bytes'] = len((await child.stderr_task).encode())
        result['elapsed_seconds'] = round(time.monotonic() - started, 3)
        (HERE / 'installed-inspection.json').write_text(json.dumps(receipt, indent=2) + '\n')
        print(json.dumps({k: v for k, v in result.items() if k != 'loaded_file_sources'}), flush=True)

async def run():
    for name in ('nra-architecture', 'openhcs-helper2'):
        await inspect(name)

asyncio.run(run())
