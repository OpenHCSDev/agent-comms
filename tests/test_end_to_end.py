"""The real console adapter forks, routes messages, polls and acknowledges."""


import json
import os
import sys

import pytest

from agent_comms.channel_targets import CommandTarget
from agent_comms.child_process import BoundedRun
from agent_comms.cli import main
from agent_comms.comms import wire
from agent_comms.runtime import socket_path
from agent_comms.read_basis import NamedViewer
from native_backend_fixture import native_backend_fixture


@pytest.fixture
async def native(tmp_path, native_runtime_package):
    async with native_backend_fixture(tmp_path, package=native_runtime_package) as owner:
        yield owner


async def cli(programs, root, *arguments):
    # The test's own interpreter runs the console target, so the CLI and the in-process
    # wire are one runtime: a root refuses a second one (root_runtime.py:56).
    result = await BoundedRun.run(
        (sys.executable, str(programs.script(main)), '--root', str(root), *arguments),
        timeout=20, env=dict(os.environ),
    )
    assert result.outcome.successful, (result.stdout + result.stderr).decode()
    return json.loads(result.stdout)


class TestEndToEndLifecycle:
    async def test_fork_send_receive_respond(self, native, source_programs):
        await native.author_history()
        root = native.root
        await cli(source_programs, root, 'register', '--name', 'PR111',
                  '--worktree', str(native.project), '--execution', 'native')
        comms = wire(root)
        parent = comms.registry.require('PR111')
        comms.threads.attach_session(parent, str(native.session), process=parent.process_identity)
        try:
            result = await cli(
                source_programs, root, 'fork', '--name', 'kid', '--parent', 'PR111',
                '--task', 'review the diff', '--project', str(native.project),
                '--model', 'response-local/fixture', '--thinking-level', 'off',
            )
            assert result['created'] == 'kid'
            detail = await cli(source_programs, root, 'thread', '--name', 'kid')
            assert detail['parent'] == 'PR111' and detail['task'] == 'review the diff'
            assert detail['process_identity']['pid'] > 0
            await cli(source_programs, root, 'stop', '--name', 'kid')
            assert comms.registry.status('kid').declared_name == 'stopped'
            assert not socket_path(root, detail['process_identity']['pid']).exists()

            # The actual fork has been exercised. Explicit CLI consumers are
            # external participants, not extinct native processes fabricated by env.
            for name, extra in (('PR111', ()), ('kid', ('--parent', 'PR111'))):
                await cli(source_programs, root, 'register', '--name', name,
                          '--worktree', str(native.project), *extra)
            await cli(source_programs, root, 'send', '--from', 'PR111', '--to', 'kid',
                      '--body', 'please review PR 112', '--type', 'handoff')
            snapshot = await cli(source_programs, root, 'poll', '--thread', 'kid')
            assert snapshot['thread']['name'] == 'kid'
            assert [message['text'] for message in snapshot['inbox']] == ['please review PR 112']
            assert snapshot['inbox'][0]['type'] == 'handoff'
            await cli(source_programs, root, 'send', '--from', 'kid', '--to', 'PR111',
                      '--body', 'review complete, one nit', '--type', 'ack')
            assert await cli(source_programs, root, 'ack', '--thread', 'PR111') == {'acknowledged': 1}
            assert (await cli(source_programs, root, 'inbox', '--thread', 'PR111'))['messages'] == []
        finally:
            for thread in comms.registry.snapshot().threads.values():
                if thread.process_alive:
                    await cli(source_programs, root, 'stop', '--name', thread.name)

    async def test_all_channel_reaches_every_thread_via_cli(self, native, source_programs):
        for name in ('a', 'b', 'c'):
            await cli(source_programs, native.root, 'register', '--name', name,
                      '--worktree', str(native.project))
        await cli(source_programs, native.root, 'send', '--from', 'a', '--to', '#all',
                  '--body', 'heads up')
        for name in ('b', 'c'):
            inbox = await cli(source_programs, native.root, 'inbox', '--thread', name)
            assert [message['text'] for message in inbox['messages']] == ['heads up']
        assert (await cli(source_programs, native.root, 'inbox', '--thread', 'a'))['messages'] == []

    async def test_acp_client_and_cli_share_one_wire(self, native, source_programs):
        async with native.open_owner() as (agent, session):
            await cli(source_programs, native.root, 'register', '--name', 'cli-agent',
                      '--worktree', str(native.project))
            await cli(source_programs, native.root, 'send', '--from', 'cli-agent', '--to', session,
                      '--body', 'from the cli side')
            comms = agent._comms
            target = CommandTarget.named(session)
            assert [message.body for message in comms.views.history_page(
                target, viewer=NamedViewer('cli-agent')).messages] == ['from the cli side']
            await cli(source_programs, native.root, 'send', '--from', 'cli-agent', '--to', session,
                      '--body', 'roger')
            assert [message.body for message in wire(native.root).views.history_page(
                target, viewer=NamedViewer('cli-agent')).messages] == ['from the cli side', 'roger']
