"""One fresh installed configured-provider journey; never replay a failed root."""
import argparse
import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

import agent_comms
from agent_comms.acp import CommsAgent, CommsClient
from agent_comms.active_route import LocalRoute, PinnedRoute
from agent_comms.child_process import AttachedChild
from agent_comms.comms import Comms
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.native_tools import CodingTool
from agent_comms.native_settings import NativeThreadDefaults
from agent_comms.pi_commands import AgentCommsInspectContext
from agent_comms.pi_vocabulary import HighThinkingLevel
from agent_comms.thread_management import ForkSpec
from agent_comms.threads import Thread
from agent_comms.tracked_turn import TrackedTurnSession
from agent_comms.turn_context import ToolCatalogSegment


class Client:
    def __init__(self):
        self.updates = []

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append(update.model_dump(by_alias=True, exclude_none=True)
                            if hasattr(update, 'model_dump') else update)


async def main(output, package):
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
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
    comms = Comms(PinnedRoute(root, root_id, package), runtime=runtime,
                  settings=NativeThreadDefaults.offline(arguments=('--no-extensions', '--no-prompt-templates')))
    agent = CommsAgent(comms, runtime_enabled=True, auto_wake=False,
                       private_nk_native_package=package, private_nk_wire_root_id=root_id)
    agent.on_connect(Client())
    observations, children, commands = [], [], []
    result = {'complete': False, 'core_module': agent_comms.__file__,
              'runtime': str(runtime.interpreter), 'package': str(package), 'tool_supply': observations}
    attest = TrackedTurnSession.attest
    start = AttachedChild.start
    attachment = None
    forked = None
    source = None
    before = None

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
        commands.append(tuple(command))
        return child

    try:
        with patch.object(AttachedChild, 'start', recorded):
            opened = await agent.new_session(cwd=str(project), mcp_servers=[])
            parent = comms.registry.require(opened.session_id)
            comms.registry.register(replace(parent, tags=frozenset({'runtime-check'})))
            comms.registry.declare(Thread('verifier', frozenset(), str(project)))
            instruction = ('Fresh isolated native verification after channel triage. Use read to inspect '
                'fresh-input.txt. Then use bash exactly once to run '
                "printf 'native-effect\\n' >> fresh-effect.txt . Do not retry that command. "
                'After both succeed, reply exactly NATIVE_FULL_OK. Do not inspect or change any other file.')
            comms.messaging.send_message('verifier', '#runtime-check', instruction)
            with patch.object(TrackedTurnSession, 'attest', inspected):
                await agent.inputs.drain_inbox(opened.session_id)
                task = agent.inputs.wake_tasks.get(opened.session_id)
                assert task is not None, 'No reply turn scheduled'
                await asyncio.wait_for(task, 180)
            parent = comms.registry.require(opened.session_id)
            source = Path(parent.require_saved_session())
            rows = [json.loads(line) for line in source.read_text().splitlines()]
            messages = [row['message'] for row in rows if row.get('type') == 'message']
            tools = [message for message in messages if message.get('role') == 'toolResult']
            replies = [message.body for message in comms.views.channel_history('#runtime-check')
                       if message.sender == opened.session_id]
            result.update(model=parent.model, tools=[{'name': message['toolName'],
                          'error': message.get('isError', False)} for message in tools], replies=replies)
            assert len(observations) == 2 and observations[0]['tools'] == []
            assert set(observations[1]['tools']) == set(CodingTool.names())
            assert {message['toolName'] for message in tools} == {'read', 'bash'}
            assert all(not message.get('isError') for message in tools)
            assert (project / 'fresh-effect.txt').read_text() == 'native-effect\n'
            assert len(replies) == 1 and replies[0].strip() == 'NATIVE_FULL_OK'
            await agent.shutdown()
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            forked = await asyncio.to_thread(comms.threads.create, ForkSpec(
                name='runtime-fork', parent=parent.name, project=str(project),
                model=parent.model, thinking_level=parent.thinking_level))
            helper = [command for command in commands if 'agent_comms.native_fork' in command]
            assert helper == [runtime.command('-m', 'agent_comms.native_fork')]
            assert Path(forked.require_saved_session()).is_relative_to(profile)
            assert forked.session_file != str(source)
            assert hashlib.sha256(source.read_bytes()).hexdigest() == before
            result.update(fork_helper_argv=list(helper[0]), fork_session=forked.session_file)
            process = forked.require_process()
            result['fork_owner_argv'] = Path(f'/proc/{process.pid}/cmdline').read_bytes().split(b'\0')[:5]
            result['fork_owner_argv'] = [value.decode() for value in result['fork_owner_argv']]
            assert result['fork_owner_argv'][:2] == [str(runtime.interpreter), '-I']
            attachment = CommsClient(comms, runtime_enabled=True,
                private_nk_native_package=package, private_nk_wire_root_id=root_id)
            client = Client()
            attachment.on_connect(client)
            async with asyncio.timeout(90):
                loaded = await attachment.load_session(str(project), forked.name)
                response = await attachment.prompt(forked.name, [{'type': 'text',
                    'text': 'Fresh fork verification. Do not use tools. Reply exactly NATIVE_FORK_OK.'}])
            result['fork_response'] = response.stop_reason
            result['fork_loaded'] = bool(loaded)
            current = comms.registry.require(forked.name)
            journal = [json.loads(line) for line in Path(current.require_saved_session()).read_text().splitlines()]
            final = [message for row in journal if row.get('type') == 'message'
                     for message in (row['message'],) if message.get('role') == 'assistant'
                     and message.get('stopReason') == 'stop'][-1]
            text = ''.join(part.get('text', '') for part in final.get('content', []) if part.get('type') == 'text')
            result['fork_answer'] = text
            assert text.strip() == 'NATIVE_FORK_OK'
            assert hashlib.sha256(source.read_bytes()).hexdigest() == before
            result['complete'] = True
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
        if forked is not None:
            try:
                await asyncio.to_thread(comms.owners.stop, forked.name)
            except BaseException as error:
                cleanup.append(repr(error))
        result['children_retired'] = all(child.returncode is not None for child in children)
        result['cleanup_errors'] = cleanup
        if source is not None and before is not None:
            result['parent_unchanged'] = hashlib.sha256(source.read_bytes()).hexdigest() == before
        (output / 'result.json').write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--package', type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.output, args.package))
