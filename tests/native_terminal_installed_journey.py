"""One new configured-provider ACP input, one local effect and one final reply.

Run against an isolated installed wheel with an explicit private root/package.
Never imports or replays a prior saved input. The caller supplies an isolated
copy of provider configuration, including the native models-store.json;
the original configuration is never written.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch

from agent_comms import activity, runtime_updates
from agent_comms.launch_runtime import ThreadLaunch
from agent_comms.acp import CommsAgent
from agent_comms.comms import Comms
from agent_comms.threads import Thread
from agent_comms.wire_record import WireRecord


class Client:
    def __init__(self):
        self.updates = []

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append(update.model_dump(by_alias=True, exclude_none=True))


async def main(output, package, launcher, provider, model, thinking, read_only=False):
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    project = output / 'project'
    project.mkdir(mode=0o700)
    comms = Comms(output / 'wire')
    comms.messaging.initialize_private_initial_protocol()
    ThreadLaunch.strip(os.environ)
    os.environ.update({
        'AGENT_COMMS_ROOT': str(comms.root),
    })
    agent = CommsAgent(
        comms, runtime_enabled=True, auto_wake=False,

        agent_bin=str(launcher), agent_args=[
            '--provider', provider, '--model', model, '--thinking', thinking,
            '--no-extensions', '--no-skills', '--no-context-files', '--tools', 'bash',
        ],
    )
    assert comms.owners.launch_authority.package_for(str(launcher)).path == package.absolute()
    client = Client()
    agent.on_connect(client)
    effect = project / 'one-effect.txt'
    entered = []
    original_enter = activity.ThreadOutput.enter
    assert runtime_updates.ThreadOutput.enter is original_enter

    def observe_enter(output, phase):
        original_enter(output, phase)
        entered.append({'phase': phase.declared_name, 'state': phase.activity_state.value,
                        'output': type(output).__name__, 'thread': output.activity_thread})
    try:
        await agent._runtime.start()
        opened = await agent.new_session(cwd=str(project), mcp_servers=[])
        comms.registry.declare(comms.registry.new_thread('verifier', frozenset(), str(project)))
        command = "printf 'NATIVE_PHASE_TOOL_OK\\n'" if read_only else "printf 'effect\\n' >> one-effect.txt"
        instruction = (
            "Isolated native terminal verification. Use bash exactly once to run this "
            f"command in the current directory: {command} . "
            "Do not read or modify any other file and do not retry the command. "
            "After it succeeds, reply with exactly NATIVE_TERMINAL_OK."
        )
        original = comms.messaging.send('verifier', opened.session_id, instruction).message
        with patch.object(activity.ThreadOutput, 'enter', observe_enter):
            await agent.turns.run_agent_turn(
                opened.session_id, opened.session_id, instruction, origins=(original,),
            )
        if not read_only:
            assert effect.read_text() == 'effect\n'
        assert any(entry['phase'] == 'tool_running' for entry in entered), entered
        assert any(entry['phase'] == 'model_wait' for entry in entered), entered
        activity_events = [json.loads(line) for line in comms.agents.activity.path.read_text().splitlines()]
        assert activity_events[-1]['state'] == 'idle', activity_events
        thread = comms.registry.require(opened.session_id)
        saved = Path(thread.session_file)
        journal = [json.loads(line) for line in saved.read_text().splitlines()]
        messages = [row['message'] for row in journal if row.get('type') == 'message']
        users = [message for message in messages if message['role'] == 'user']
        tools = [message for message in messages if message['role'] == 'toolResult']
        finals = [message for message in messages if message.get('stopReason') == 'stop']
        assert len(users) == len(tools) == len(finals) == 1
        if read_only:
            assert tools[0]['isError'] is False
            assert 'NATIVE_PHASE_TOOL_OK' in ''.join(item.get('text', '') for item in tools[0]['content'])
        assert ''.join(item.get('text', '') for item in finals[0]['content']).strip() == 'NATIVE_TERMINAL_OK'
        published = [json.loads(line) for line in comms.bus.log.path.read_text().splitlines()]
        # Only this small, newly created private bus is inspected.
        replies = [message for row in published
                   for message in WireRecord.public_from_wire(row).messages()
                   if message.sender == opened.session_id]
        assert len(replies) == 1, replies
        assert replies[0].target == 'verifier'
        assert replies[0].body.strip() == 'NATIVE_TERMINAL_OK'
        (output / 'updates.json').write_text(json.dumps(client.updates, indent=2))
        (output / 'result.json').write_text(json.dumps({
            'provider': provider, 'model': model, 'thinking': thinking,
            'user_inputs': len(users), 'tool_results': len(tools),
            'successful_native_finals': len(finals), 'effect_lines': 0 if read_only else 1,
            'published_replies': len(replies), 'saved_session': str(saved),
            'thread_output_enter': entered, 'activity': activity_events,
        }, indent=2))
        print((output / 'result.json').read_text())
    finally:
        await agent.shutdown()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--launcher', type=Path, required=True)
    parser.add_argument('--provider', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--thinking', required=True)
    parser.add_argument('--read-only', action='store_true')
    args = parser.parse_args()
    asyncio.run(main(args.output, args.package, args.launcher, args.provider, args.model, args.thinking, args.read_only))
