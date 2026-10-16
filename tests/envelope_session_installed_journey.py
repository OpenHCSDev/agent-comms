"""One installed ACP/native journey; localhost replies, real retained sessions."""
from __future__ import annotations

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


def user_text(request):
    message = next(row for row in reversed(request['messages']) if row['role'] == 'user')
    content = message['content']
    return content if isinstance(content, str) else ''.join(row.get('text', '') for row in content)


def system_text(request):
    return '\n'.join(row['content'] for row in request['messages'] if row['role'] == 'system')


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
            async with fixture.open_owner(runtime_enabled=True) as (agent, name):
                comms = agent._comms
                result['thread'] = name

                async def prompt(label, text):
                    before_posts = fixture.provider.posts
                    response = await agent.prompt(name, [TextContentBlock(type='text', text=text)])
                    assert response.stop_reason == 'end_turn', response
                    request = fixture.provider.requests[-1]
                    body = user_text(request)
                    (root / f'{label}-request.json').write_text(json.dumps(request, indent=2))
                    assert text in body, body[-100:]
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
                    result['native_pids'] = [original_child.pid, reopened.pid]
                    result['compaction_and_native_resume'] = True
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
    options = parser.parse_args()
    # This is a fresh isolated owner, never the inherited live worker.
    os.environ.pop('AGENT_COMMS_THREAD', None)
    asyncio.run(run(options.root, after=options.after))
