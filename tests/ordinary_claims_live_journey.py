"""Real configured Pi + scratch wire + stdio ACP; no fake Core/native objects.

Run with the desired checkout on PYTHONPATH and --evidence under persistent
storage. Uses the configured provider/model, writes scratch files, and explicitly
cancels one turn and kills one acquired scratch native child. Never replays input.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import sys

from acp import RequestError, spawn_agent_process
from acp.schema import TextContentBlock
from agent_comms.acp import CommsAgent
from agent_comms.comms import Comms
from agent_comms.pi_vocabulary import HighThinkingLevel
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.session_load import ExistingSessionLoadPlan
from agent_comms.storage_rules import StorageRoots


class Observer:
    """A real ACP client's notification sink, not an execution substitute."""
    def __init__(self):
        self.updates = []

    def on_connect(self, connection):
        self.connection = connection

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append(update.model_dump(by_alias=True, exclude_none=True))

    async def request_permission(self, **kwargs):
        raise AssertionError("No permission dialog belongs to this scratch operation")


async def until(predicate, task=None):
    async with asyncio.timeout(90):
        while not predicate():
            if task is not None and task.done():
                await task
                raise AssertionError("Turn ended before the requested scratch bash started")
            await asyncio.sleep(.05)


@asynccontextmanager
async def attachment(comms, project, name, env):
    observer = Observer()
    async with spawn_agent_process(observer, sys.executable, '-m', 'agent_comms.acp',
                                   env=env, cwd=project) as (client, process):
        await client.initialize(protocol_version=1, client_capabilities={
            'fs': {'readTextFile': True, 'writeTextFile': True}, 'terminal': True,
        })
        binding = comms.registry.snapshot().owner_binding(name)
        await client.load_session(cwd=str(project), session_id=name, mcp_servers=[],
                                  **ExistingSessionLoadPlan(binding).metadata())
        yield client, observer


async def journey(arguments):
    stage = arguments.evidence.absolute()
    stage.mkdir(parents=True, mode=0o700, exist_ok=False)
    project = stage / 'project'
    project.mkdir(mode=0o700)
    root = stage / 'wire'
    for key in tuple(os.environ):
        if key.startswith(('PI_', 'AGENT_COMMS_')):
            del os.environ[key]
    os.environ.update(AGENT_COMMS_ROOT=str(root),
                      AGENT_COMMS_NATIVE_CONFIG_DIR=str(Path.home() / '.pi/agent'),
                      PI_CODING_AGENT_DIR=str(Path.home() / '.pi/agent'))
    env = dict(os.environ)
    comms = Comms(root)
    comms.messaging.initialize_private_initial_protocol()
    name = 'scratch-sleeper'
    comms.registry.declare(replace(comms.registry.new_thread(
        name, frozenset(), str(project), model=arguments.model, thinking_level=HighThinkingLevel),
        storage_roots=StorageRoots(scratch=stage)))
    agent = CommsAgent(comms, agent_bin=arguments.native_launcher,
                       agent_args=['--offline', '--no-skills', '--no-context-files',
                                   '--no-prompt-templates'],
                       runtime_enabled=True, auto_wake=False)
    results = []
    try:
        comms.owners.require_private_launch().prepare_owner(
            comms.registry.require(name), arguments.native_launcher, LaunchRuntime.running())
        await agent.sessions.start_owner(str(project), name)
        async with attachment(comms, project, name, env) as (client, observer):
            for outcome in ('success', 'cancelled', 'failed'):
                target = project / f'{outcome}.txt'
                marker = project / f'{outcome}.marker'
                command = f"printf running > {marker}; sleep 30; printf finished > {marker}"
                prompt = (f'This is an isolated runtime acceptance task. Use the write tool to write '
                          f'exactly "value = 2\\n" into {target}. Then use bash to run this exact '
                          f'command, with a timeout over 40 seconds: {command}\n'
                          'Do not start background work. After bash returns, reply only SLEEP_FINISHED.')
                operation = asyncio.create_task(client.prompt(name, [TextContentBlock(
                    type='text', text=prompt)]))
                await until(lambda: marker.exists() and marker.read_text() == 'running', operation)
                assert target.exists(), 'Pi must acquire and execute write before sleeping'
                acquired = comms.bus.log.claim_projection()[str(target)]
                turn = comms.registry.require(name).require_turn_lease()
                # The second connection executes Toad's actual initialize/load plan.
                async with attachment(comms, project, name, env):
                    assert comms.registry.require(name).require_turn_lease() == turn
                    assert not operation.done()
                assert comms.registry.require(name).require_turn_lease() == turn
                print(f'{outcome}: ATTACH_AND_DETACH_DID_NOT_CANCEL turn={turn.turn_id}', flush=True)
                if outcome == 'cancelled':
                    await client.cancel(name)
                elif outcome == 'failed':
                    child = agent.turns.persistent_backends[name].child_state.child.proc
                    child.identity.signal(signal.SIGKILL)
                try:
                    response = await asyncio.wait_for(operation, 90)
                    stop = response.stop_reason
                except RequestError as error:
                    stop = f'{type(error).__name__}: {error}'
                    if outcome != 'failed':
                        raise
                await until(lambda: not comms.registry.require(name).turn_record.executing)
                current = comms.bus.log.claim_projection().get(str(target))
                row = dict(outcome=outcome, stop=stop, claim_remaining=current is not None,
                           acquired_seq=acquired.seq, actual_file=target.read_text(),
                           marker=marker.read_text(), turn=turn.turn_id)
                results.append(row)
                (stage / 'result.json').write_text(json.dumps(results, indent=2))
                print(json.dumps(row), flush=True)
                assert row['claim_remaining'] is arguments.expect_stale
                if outcome == 'success':
                    assert stop == 'end_turn' and row['marker'] == 'finished'
                if outcome == 'cancelled':
                    assert stop == 'cancelled'
            (stage / 'updates.json').write_text(json.dumps(observer.updates))
    finally:
        await agent.shutdown()
    print('PASS_REAL_PI_ACP_JOURNEY', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--model', default='openai-codex/gpt-6.1-sol')
    parser.add_argument('--native-launcher', required=True)
    parser.add_argument('--expect-stale', action='store_true')
    asyncio.run(journey(parser.parse_args()))
