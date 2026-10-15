"""One new configured-provider ACP input, one local effect and one final reply.

Run against an isolated installed wheel with an explicit private root/package.
Never imports or replays a prior saved input. The caller supplies an isolated
copy of provider configuration; the original configuration is never written.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path

from acp.schema import TextContentBlock
from agent_comms.acp import CommsAgent
from agent_comms.comms import Comms
from agent_comms.private_nk_entrypoint import PACKAGE_ENV, ROOT_ID_ENV


class Client:
    def __init__(self):
        self.updates = []

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append(update.model_dump(by_alias=True, exclude_none=True))


async def main(output, package, provider, model, thinking):
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    project = output / 'project'
    project.mkdir(mode=0o700)
    comms = Comms(output / 'wire')
    root_id = comms.messaging.initialize_private_initial_protocol()
    os.environ.update({
        'AGENT_COMMS_ROOT': str(comms.root), ROOT_ID_ENV: root_id,
        PACKAGE_ENV: str(package),
    })
    agent = CommsAgent(
        comms, runtime_enabled=True, auto_wake=False,
        private_nk_native_package=package, private_nk_wire_root_id=root_id,
        agent_bin='pi', agent_args=[
            '--provider', provider, '--model', model, '--thinking', thinking,
            '--no-extensions', '--no-skills', '--no-context-files', '--tools', 'bash',
        ],
    )
    client = Client()
    agent.on_connect(client)
    effect = project / 'one-effect.txt'
    try:
        await agent._runtime.start()
        opened = await agent.new_session(cwd=str(project), mcp_servers=[])
        response = await agent.prompt(opened.session_id, [TextContentBlock(type='text', text=(
            "Isolated native terminal verification. Use bash exactly once to run this "
            "command in the current directory: printf 'effect\\n' >> one-effect.txt . "
            "Do not read or modify any other file and do not retry the command. "
            "After it succeeds, reply with exactly NATIVE_TERMINAL_OK."
        ))])
        assert response.stop_reason == 'end_turn', response
        assert effect.read_text() == 'effect\n'
        thread = comms.registry.require(opened.session_id)
        saved = Path(thread.session_file)
        journal = [json.loads(line) for line in saved.read_text().splitlines()]
        messages = [row['message'] for row in journal if row.get('type') == 'message']
        users = [message for message in messages if message['role'] == 'user']
        tools = [message for message in messages if message['role'] == 'toolResult']
        finals = [message for message in messages if message.get('stopReason') == 'stop']
        assert len(users) == len(tools) == len(finals) == 1
        assert ''.join(item.get('text', '') for item in finals[0]['content']).strip() == 'NATIVE_TERMINAL_OK'
        (output / 'updates.json').write_text(json.dumps(client.updates, indent=2))
        (output / 'result.json').write_text(json.dumps({
            'provider': provider, 'model': model, 'thinking': thinking,
            'user_inputs': len(users), 'tool_results': len(tools),
            'successful_native_finals': len(finals), 'effect_lines': 1,
            'acp_stop_reason': response.stop_reason, 'saved_session': str(saved),
        }, indent=2))
        print((output / 'result.json').read_text())
    finally:
        await agent.shutdown()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--provider', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--thinking', required=True)
    args = parser.parse_args()
    asyncio.run(main(args.output, args.package, args.provider, args.model, args.thinking))
