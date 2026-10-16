"""Fresh installed configured-provider journey. Never reuse an output root."""
import argparse
import asyncio
from contextlib import redirect_stdout
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
from unittest.mock import patch
import zipfile

import agent_comms
from acp.schema import TextContentBlock
from agent_comms.acp import CommsAgent, CommsClient
from agent_comms.active_route import LocalRoute, PinnedRoute
from agent_comms.child_process import AttachedChild
from agent_comms.cli import main as cli_main
from agent_comms.comms import Comms
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.native_arguments import NativeArguments
from agent_comms.native_tools import CodingTool
from agent_comms.native_settings import NativeThreadDefaults
from agent_comms.pi_commands import AgentCommsInspectContext
from agent_comms.private_nk_entrypoint import PrivateNkLaunch
from agent_comms.threads import Thread
from agent_comms.tracked_turn import TrackedTurnSession
from agent_comms.turn_context import ToolCatalogSegment


class Client:
    def __init__(self):
        self.updates = []

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append(update.model_dump(by_alias=True, exclude_none=True)
                            if hasattr(update, 'model_dump') else update)


def journal(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require_installed_wheel(wheel):
    site = Path(agent_comms.__file__).parent.parent
    matched = 0
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if name.startswith('agent_comms/') and not name.endswith('/'):
                assert (site / name).read_bytes() == archive.read(name), name
                matched += 1
    assert matched > 100
    return matched


async def main(output, package, wheel, core_revision):
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    result = {'complete': False, 'phase': 'setup', 'core_revision': core_revision,
              'core_module': agent_comms.__file__, 'wheel_sha256': digest(wheel)}
    (output / 'result.json').write_text(json.dumps(result, indent=2))
    result['installed_files_matched'] = require_installed_wheel(wheel)
    project = output / 'project'
    project.mkdir(mode=0o700)
    (project / 'fresh-input.txt').write_text('NATIVE_RUNTIME_MARKER\n')
    profile = output / 'profile'
    profile.mkdir(mode=0o700)
    os.environ['PI_CODING_AGENT_DIR'] = str(profile)
    os.environ['AGENT_COMMS_NATIVE_CONFIG_DIR'] = str(Path.home() / '.pi/agent')
    runtime = LaunchRuntime.running()
    root = output / 'wire'
    root_id = Comms(LocalRoute(root), runtime=runtime).messaging.initialize_private_initial_protocol()
    PrivateNkLaunch(root, root_id, package, None).apply_environment(os.environ)
    settings = NativeThreadDefaults.offline(arguments=NativeArguments.parse(('--no-prompt-templates',)))
    comms = Comms(PinnedRoute(root, root_id, package), runtime=runtime, settings=settings)
    agent = CommsAgent(comms, runtime_enabled=True, auto_wake=True,
                       private_nk_native_package=package, private_nk_wire_root_id=root_id)
    agent.on_connect(Client())
    observations, children, commands = [], [], []
    result.update(runtime=str(runtime.interpreter), package=str(package), tool_supply=observations,
                  fork_rows=[], launch_commands=commands)
    attest = TrackedTurnSession.attest
    start = AttachedChild.start
    attachment = None
    source = None
    before = None
    fork_names = ('runtime-fork-default', 'runtime-fork-explicit')

    async def inspected(turn):
        await attest(turn)
        response = await AgentCommsInspectContext().exchange(turn.native.reader, turn.native.proc.stdin)
        assert response.success, response.error
        catalogs = [segment for segment in response.data.require_payload().segments
                    if isinstance(segment, ToolCatalogSegment)]
        assert len(catalogs) == 1
        observations.append({'input_id': turn.command.input_id,
            'mode': type(turn.selected_tool_mode).__name__, 'pid': turn.native.proc.pid,
            'tools': [tool['name'] for tool in catalogs[0].tools]})

    async def recorded(command, **options):
        child = await start(command, **options)
        children.append(child)
        commands.append(tuple(map(str, command)))
        return child

    def fork_cli(name, selection):
        arguments = (*selection.cli_arguments, '--root', str(root), 'fork',
                     '--name', name, '--parent', parent.name, '--project', str(project))
        captured = io.StringIO()
        with redirect_stdout(captured):
            code = cli_main(arguments)
        return code, captured.getvalue(), arguments

    try:
        with patch.object(AttachedChild, 'start', recorded):
            opened = await agent.new_session(cwd=str(project), mcp_servers=[])
            parent = comms.registry.require(opened.session_id)
            comms.registry.register(replace(parent, tags=frozenset({'runtime-check'}), auto_title_pending=False,
                task='Own #runtime-check requests to verify fresh-input.txt and create fresh-effect.txt once in this isolated project.'))
            comms.registry.declare(Thread('verifier', frozenset(), str(project)))
            instruction = (f'{parent.name}: perform your assigned isolated native verification. Use read to inspect '
                'fresh-input.txt. Then use bash exactly once to run '
                "`printf 'native-effect\\n' >> fresh-effect.txt`. Do not retry that command. "
                'After both succeed, include NATIVE_FULL_OK in your nonempty reply. Do not inspect or change any other file.')
            result['phase'] = 'triage-full'
            with patch.object(TrackedTurnSession, 'attest', inspected):
                comms.messaging.send_message('verifier', '#runtime-check', instruction)
                await agent.inputs.drain_inbox(opened.session_id)
                task = agent.inputs.wake_tasks.get(opened.session_id)
                assert task is not None, 'No reply turn scheduled'
                await asyncio.wait_for(task, 180)
            parent = comms.registry.require(opened.session_id)
            source = Path(parent.require_saved_session())
            messages = [row['message'] for row in journal(source) if row.get('type') == 'message']
            tools = [message for message in messages if message.get('role') == 'toolResult']
            replies = [message.body for message in comms.views.channel_history('#runtime-check')
                       if message.sender == opened.session_id]
            result.update(model=parent.model, tools=[{'name': message['toolName'],
                          'error': message.get('isError', False)} for message in tools], replies=replies)
            result['triage_full'] = (
                len(observations) == 2 and observations[0]['tools'] == []
                and set(CodingTool.names()) <= set(observations[1]['tools'])
                and 'comms_send' in observations[1]['tools']
                and {message['toolName'] for message in tools} == {'read', 'bash'}
                and all(not message.get('isError') for message in tools)
                and (project / 'fresh-effect.txt').is_file()
                and (project / 'fresh-effect.txt').read_text() == 'native-effect\n'
                and len(replies) == 1 and bool(replies[0].strip()) and 'NATIVE_FULL_OK' in replies[0])
            await agent.shutdown()
            before = digest(source)
            selections = (settings, replace(settings, arguments=settings.arguments.with_model(
                parent.model).with_thinking('off')))
            for name, selection in zip(fork_names, selections, strict=True):
                result['phase'] = name
                code, text, arguments = await asyncio.to_thread(fork_cli, name, selection)
                result.setdefault('fork_cli', []).append({'name': name, 'status': code,
                                                          'output': text, 'argv': arguments})
                assert code == 0, text
                forked = comms.registry.require(name)
                saved = Path(forked.require_saved_session())
                row = {'name': name, 'model': forked.model,
                       'thinking_level': forked.thinking_level.declared_name,
                       'session_file': str(saved), 'source_session': str(source)}
                result['fork_rows'].append(row)
                expected = selection.startup_arguments()
                assert forked.model == expected.model
                assert row['thinking_level'] == expected.thinking
                assert saved.is_relative_to(output) and saved != source
                assert digest(source) == before
                process = forked.require_process()
                row['owner_argv'] = [value.decode() for value in
                    Path(f'/proc/{process.pid}/cmdline').read_bytes().split(b'\0') if value]
                assert row['owner_argv'][:2] == [str(runtime.interpreter), '-I']
                attachment = CommsClient(comms, runtime_enabled=True,
                    private_nk_native_package=package, private_nk_wire_root_id=root_id)
                client = Client()
                attachment.on_connect(client)
                marker = 'NATIVE_DEFAULT_FORK_OK' if name == fork_names[0] else 'NATIVE_EXPLICIT_FORK_OK'
                async with asyncio.timeout(90):
                    loaded = await attachment.load_session(str(project), forked.name)
                    response = await attachment.prompt(forked.name, [TextContentBlock(type='text',
                        text=f'Fresh fork verification. Do not use tools. Include {marker} in your nonempty reply.')])
                row.update(stop_reason=response.stop_reason, loaded=bool(loaded))
                rows = journal(saved)
                final = [item['message'] for item in rows if item.get('type') == 'message'
                         and item['message'].get('role') == 'assistant'
                         and item['message'].get('stopReason') == 'stop'][-1]
                row['answer'] = ''.join(part.get('text', '') for part in final.get('content', [])
                                        if part.get('type') == 'text')
                assert row['answer'].strip() and marker in row['answer']
                levels = [item['thinkingLevel'] for item in rows if item.get('type') == 'thinking_level_change']
                row['saved_thinking_level'] = levels[-1]
                assert levels[-1] == expected.thinking
                assert digest(source) == before
                await attachment.shutdown()
                attachment = None
                await asyncio.to_thread(comms.owners.stop, name)
            helpers = [command for command in commands if 'agent_comms.native_fork' in command]
            result['fork_helpers'] = helpers
            assert helpers == [runtime.command('-m', 'agent_comms.native_fork')] * 2
            result['complete'] = result['triage_full']
            result['phase'] = 'finished'
    except BaseException as error:
        result['failure'] = repr(error)
        raise
    finally:
        cleanup = []
        for owner in (attachment, agent):
            if owner is not None:
                try:
                    await owner.shutdown()
                except BaseException as error:
                    cleanup.append(repr(error))
        for name in fork_names:
            try:
                owner = comms.registry.all_threads().get(name)
                if owner is not None and owner.process_alive:
                    await asyncio.to_thread(comms.owners.stop, name)
            except BaseException as error:
                cleanup.append(repr(error))
        result['children_retired'] = all(child.returncode is not None for child in children)
        result['cleanup_errors'] = cleanup
        result['complete'] = result['complete'] and result['children_retired'] and not cleanup
        if source is not None and before is not None:
            result['parent_unchanged'] = digest(source) == before
        (output / 'result.json').write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--wheel', type=Path, required=True)
    parser.add_argument('--core-revision', required=True)
    args = parser.parse_args()
    asyncio.run(main(args.output, args.package, args.wheel, args.core_revision))
