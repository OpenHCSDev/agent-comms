"""Original owner IPC and registry proof, without provider calls or input."""
import asyncio
from dataclasses import replace
import json
import time

import pytest

from agent_comms.comms import wire
from agent_comms.runtime_requests import ProjectRuntimeRequest, RuntimeRequest
from agent_comms.thread_presentation import LiveThreadOwnerBinding
from delivery_owner_fixture import canonical_agent


async def test_current_project_observation_keeps_original_binding_across_rename_and_project_change(tmp_path):
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    comms = wire(tmp_path / 'wire')
    owner = canonical_agent(comms, agent_bin='/bin/echo', agent_args=[], runtime_enabled=True)
    try:
        session = (await owner.new_session(str(first))).session_id
        thread = comms.registry.require(session)
        request = ProjectRuntimeRequest.for_native(comms.registry.snapshot(), thread)
        assert RuntimeRequest.from_wire(request.to_wire()) == request
        environment = owner.turns.native_environment(thread, str(first))
        assert json.loads(environment['AGENT_COMMS_PROJECT_REQUEST']) == request.to_wire()
        durations = []

        async def query(command=request):
            started = time.perf_counter_ns()
            reader, writer = await asyncio.open_unix_connection(environment['AGENT_COMMS_PROJECT_SOCKET'])
            try:
                writer.write((json.dumps(command.to_wire()) + '\n').encode())
                await writer.drain()
                reply = json.loads(await reader.readline())
                durations.append((time.perf_counter_ns() - started) / 1e6)
                return reply
            finally:
                writer.close()
                await writer.wait_closed()

        assert (await query())['result']['worktree'] == str(first)
        comms.threads.rename_managed_thread(thread.name, 'project-renamed', owner_pid=thread.pid)
        assert (await query())['result']['worktree'] == str(first)
        comms.threads.set_project('project-renamed', str(second))
        assert (await query())['result']['worktree'] == str(second)
        stale_generation = replace(request, binding=replace(request.binding,
            owner=replace(request.binding.owner, generation=request.binding.owner.generation + 1)))
        assert 'error' in await query(stale_generation)
        stale_process = replace(request, binding=LiveThreadOwnerBinding(request.binding.owner,
            replace(request.binding.process, start_time=request.binding.process.start_time + 1)))
        assert 'error' in await query(stale_process)
        assert 'error' in await query(replace(request, thread='foreign-project'))
        print('original_project_observation_ms=' + json.dumps(durations))
        assert comms.registry.require('project-renamed').worktree == str(second)
        assert comms.registry.require('project-renamed').turn_lease is None
    finally:
        await owner.shutdown()
