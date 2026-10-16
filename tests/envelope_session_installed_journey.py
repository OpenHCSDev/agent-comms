"""One installed ACP/native journey; localhost replies, real retained sessions."""
from __future__ import annotations

from agent_comms.runtime_socket import RuntimeSocket
from agent_comms.pi_vocabulary import OffThinkingLevel
from agent_comms.read_basis import ExplicitWatchAcknowledgment
from agent_comms.tool_caller import NoThreadCaller

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

import agent_comms
from acp.schema import TextContentBlock
from agent_comms.agent_backend import PlacedCompaction
from agent_comms.activity import ActivityState
from agent_comms.child_process import ProcessIdentity
from agent_comms.relationships import RelationshipEdit
from agent_comms.threads import Thread
from native_backend_fixture import native_backend_fixture
from explicit_private_owner_installed_journey import Subscriber


def message_text(message):
    """Decode captured provider text at this measurement boundary only."""
    content = message['content']
    return content if isinstance(content, str) else ''.join(row.get('text', '') for row in content)


def user_text(request):
    return message_text(next(row for row in reversed(request['messages']) if row['role'] == 'user'))


def system_text(request):
    return '\n'.join(message_text(row) for row in request['messages'] if row['role'] == 'system')


async def run(root: Path, *, after: bool):
    assert 'site-packages' in Path(agent_comms.__file__).parts, agent_comms.__file__
    root.mkdir(mode=0o700)
    result = {'state': 'unfinished', 'core': agent_comms.__file__, 'python': sys.executable,
              'after': after, 'paid_provider_calls': 0, 'measurements': {}}
    try:
        async with native_backend_fixture(root) as fixture:
            settings = fixture.config / 'settings.json'
            data = json.loads(settings.read_text())
            data['compaction']['keepRecentTokens'] = 16
            settings.write_text(json.dumps(data))
            await fixture.author_history()
            subscriber = Subscriber()
            children = []
            async with fixture.open_owner(runtime_enabled=True, client=subscriber) as (agent, name):
                comms = agent._comms
                result['thread'] = name

                async def prompt(label, text):
                    before_posts = fixture.provider.posts
                    before_updates = len(subscriber.updates)
                    response = await agent.prompt(name, [TextContentBlock(type='text', text=text)])
                    assert response.stop_reason == 'end_turn', response
                    request = fixture.provider.requests[-1]
                    body = user_text(request)
                    assert 'One fact. One owner. Derive the rest.' in system_text(request)
                    (root / f'{label}-request.json').write_text(json.dumps(request, indent=2))
                    assert text in body, body[-100:]
                    assert 'Write the answer as your reply text' in body
                    assert fixture.provider.text in json.dumps(subscriber.updates[before_updates:])
                    result['measurements'][label] = {
                        'envelope_bytes': len(body.encode()) - len(text.encode()),
                        'input_bytes': len(body.encode()),
                        'system_bytes': len(system_text(request).encode()),
                        'posts': fixture.provider.posts - before_posts,
                    }
                    if after:
                        owner = comms.registry.require(name)
                        system = system_text(request)
                        assert f"thread {owner.name!r}" in system
                        assert repr(owner.worktree) in system
                        assert 'Maintenance results' in system
                        assert 'Maintenance results' not in body
                        assert 'overrides inherited' not in body
                    return body

                await prompt('short', 'ping')
                comms.registry.register(Thread('collaborator895', frozenset(), str(fixture.project),
                    process_identity=ProcessIdentity.capture(os.getpid())))
                comms.agents.set_activity('collaborator895', ActivityState.WORKING, 'Review envelope')
                comms.relationships.edit(name, RelationshipEdit.decode('add'), 'collaborator895',
                                         'Current independent review')
                body = await prompt('collaboration', 'review')
                assert 'collaborator895' in body
                if after:
                    thread = comms.registry.require(name)
                    backend = agent.turns.backend_for(name, thread)
                    original_child = backend.custody.idle().child.proc
                    children.append(original_child)
                    compacted = await backend.compact(thread, 'Preserve the task. Do not invent identity.')
                    assert isinstance(compacted, PlacedCompaction), compacted
                    result['compaction_summary'] = compacted.summary
                    await prompt('after-compaction', 'compact-check895')
                    assert backend.custody.idle().child.proc is original_child
                    await backend.close_idle()
                    assert not original_child.alive()
                    await prompt('after-resume', 'resume-check895')
                    reopened = backend.custody.idle().child.proc
                    assert reopened is not original_child and reopened.alive()
                    children.append(reopened)
                    result['native_pids'] = [original_child.pid, reopened.pid]
                    result['compaction_and_native_resume'] = True
                    project = root / 'changed-project'
                    project.mkdir(mode=0o700)
                    changed = comms.threads.set_project(name, str(project))
                    assert changed.changed
                    await prompt('after-project-change', 'project-check895')
                    moved = backend.custody.idle().child.proc
                    assert moved is not reopened and not reopened.alive()
                    children.append(moved)
                    result['project_change'] = True
                result['state'] = 'PASS'
            assert all(child.retired for child in children)
            result['all_native_children_retired'] = True
    except BaseException as error:
        result.update(state='FAILED_NO_REPLAY', error=repr(error))
        raise
    finally:
        (root / 'result.json').write_text(json.dumps(result, indent=2))
        print(json.dumps(result), flush=True)


async def detached(root: Path):
    """Actual installed CLI worker and two stdio-ACP attachments, no live roots."""
    from acp import spawn_agent_process
    from agent_comms.acp_extension import CompactRequest, encode_request
    from agent_comms.comms import Comms

    from contextlib import aclosing
    from explicit_private_owner_installed_journey import cli

    assert 'site-packages' in Path(agent_comms.__file__).parts
    root.mkdir(mode=0o700)
    result = {'state': 'unfinished', 'core': agent_comms.__file__,
              'thread': 'worker895', 'worker_pids': [], 'paid_provider_calls': 0}
    try:
        async with native_backend_fixture(root) as fixture:
            settings = fixture.config / 'settings.json'
            data = json.loads(settings.read_text())
            data['compaction']['keepRecentTokens'] = 16
            settings.write_text(json.dumps(data))
            await fixture.author_history()
            comms = Comms(fixture.comms.route, runtime=fixture.comms.runtime,
                          settings=fixture.comms.settings)
            comms.registry.declare(Thread('worker895', frozenset(), str(fixture.project),
                session_file=str(fixture.session), model='response-local/fixture', thinking_level=OffThinkingLevel))
            env = comms.runtime.environment(os.environ)
            with comms.bus.log.locked():
                root_id = comms.bus.log.read_metadata_unlocked().root_id
            env.update(AGENT_COMMS_ROOT=str(fixture.root),
                AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=root_id,
                AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=os.environ['PI_COMPACTION_TEST_PACKAGE'],
                AGENT_COMMS_NATIVE_CONFIG_DIR=str(fixture.config),
                PI_CODING_AGENT_DIR=str(fixture.config))
            for phase in ('compaction', 'resume'):
                try:
                    status, reply, stderr = await cli(env, root, ['--root', str(fixture.root), *comms.settings.cli_arguments],
                                                      {'name': 'worker895'})
                    assert status == 0 and reply['launched'], (reply, stderr)
                    owner = comms.registry.require('worker895')
                    assert owner.pid == reply['pid'] and owner.pid != os.getpid()
                    result['worker_pids'].append(owner.pid)
                    async with asyncio.timeout(20):
                        while not RuntimeSocket.path_for(comms.root, owner.pid).exists():
                            assert owner.process_alive
                            await asyncio.sleep(.05)
                    subscriber = Subscriber()
                    async with spawn_agent_process(subscriber,
                            *comms.runtime.command('-m', 'agent_comms.acp', *comms.settings.cli_arguments),
                            env=env, cwd=fixture.project) as (connection, process):
                        async with asyncio.timeout(40):
                            await connection.initialize(protocol_version=1)
                            await connection.load_session(cwd=str(fixture.project), session_id='worker895', mcp_servers=[])
                            if phase == 'compaction':
                                response = await connection.prompt('worker895', [TextContentBlock(type='text', text='before-compact895')])
                                assert response.stop_reason == 'end_turn'
                                await connection.prompt('worker895', [TextContentBlock(type='text', text='')],
                                    **encode_request(CompactRequest(instructions='Preserve the task.')))
                                assert any(json.loads(line).get('type') == 'compaction'
                                           for line in fixture.session.read_text().splitlines())
                            response = await connection.prompt('worker895', [TextContentBlock(
                                type='text', text=f'after-{phase}895')])
                            assert response.stop_reason == 'end_turn'
                        assert any(fixture.provider.text in str(update) for update in subscriber.updates)
                    request = fixture.provider.requests[-1]
                    assert 'One fact. One owner. Derive the rest.' in system_text(request)
                    assert "thread 'worker895'" in system_text(request)
                    assert repr(str(fixture.project)) in system_text(request)
                    assert 'Write the answer as your reply text' in user_text(request)
                    assert 'Maintenance results' not in user_text(request)
                    (root / f'{phase}-request.json').write_text(json.dumps(request, indent=2))
                    assert process.returncode is not None
                    result[phase] = True
                    if phase == 'resume':
                        # An agent-peer wake, not the separate human-input operation.
                        comms.registry.declare(Thread('peer895', frozenset(), str(fixture.project)))
                        sent = comms.messaging.send_message('peer895', 'worker895', 'peer-wake895')
                        async with asyncio.timeout(30), aclosing(comms.bus.watch(
                                'peer895', acknowledgment=ExplicitWatchAcknowledgment)) as replies:
                            reply = await anext(replies)
                        assert reply.sender == 'worker895' and reply.target == 'peer895'
                        assert reply.body == fixture.provider.text
                        request = fixture.provider.requests[-1]
                        assert sent.body in user_text(request)
                        assert "thread 'worker895'" in system_text(request)
                        assert 'Maintenance results' in system_text(request)
                        assert 'AGENT_COMMS_SESSION_CONTEXT' not in env
                        (root / 'selected-request.json').write_text(json.dumps(request, indent=2))
                        result['selected_addressed_reply'] = True
                finally:
                    await asyncio.to_thread(comms.owners.stop, 'worker895', caller=NoThreadCaller())
            assert len(set(result['worker_pids'])) == 2
            result['state'] = 'PASS'
    except BaseException as error:
        result.update(state='FAILED_NO_REPLAY', error=repr(error))
        raise
    finally:
        (root / 'result.json').write_text(json.dumps(result, indent=2))
        print(json.dumps(result), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    parser.add_argument('--after', action='store_true')
    parser.add_argument('--detached', action='store_true')
    options = parser.parse_args()
    # This is a fresh isolated owner, never the inherited live worker.
    os.environ.pop('AGENT_COMMS_THREAD', None)
    asyncio.run(detached(options.root) if options.detached else run(options.root, after=options.after))
