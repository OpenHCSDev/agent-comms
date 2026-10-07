"""Original project IPC and native continuation; authored localhost input only."""
import asyncio
from dataclasses import replace
import json
import time

import pytest

from agent_comms.runtime_requests import ProjectRuntimeRequest, RuntimeRequest
from agent_comms.thread_presentation import LiveThreadOwnerBinding
from native_backend_fixture import native_backend_fixture
from agent_comms.coordinator import Coordination
from agent_comms.schedule_rules import WakeScheduleCheck
from native_proof_cases import read_proof_rows
from types import SimpleNamespace


async def test_current_project_observation_keeps_original_binding_across_rename_and_project_change(tmp_path, monkeypatch):
    async with native_backend_fixture(tmp_path) as native:
        model = json.loads((native.config / 'models.json').read_text())
        origin = model['providers']['response-local']['baseUrl'].removesuffix('/v1')
        monkeypatch.setenv('AGENT_COMMS_NATIVE_ORIGIN', origin)
        for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
            monkeypatch.delenv(name, raising=False)
        first, second = native.project, tmp_path / 'second'
        second.mkdir()
        updates = []

        async def observe_reply(*, session_id, update):
            updates.append(update)

        async with native.open_owner(runtime_enabled=True, auto_wake=True,
                client=SimpleNamespace(session_update=observe_reply)) as (owner, session):
            comms = owner._comms
            thread = comms.registry.require(session)
            request = ProjectRuntimeRequest.for_native(comms.registry.snapshot(), thread)
            assert RuntimeRequest.from_wire(request.to_wire()) == request
            environment = thread.native_environment(comms.root, comms.registry.snapshot(), str(first))
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
            name = comms.threads.rename_managed_thread(thread.name, 'project-renamed', owner_pid=thread.pid).current
            assert (await query())['result']['worktree'] == str(first)
            changed = []

            class ChangeAtOriginalProviderRequest:
                async def wait(self):
                    current = await Coordination.run_worker(comms.registry.snapshot)
                    selected = current.require(name)
                    assert selected.executing
                    if not changed:
                        assert selected.worktree == str(first)
                        changed.append((selected.turn_lease,
                            await Coordination.run_worker(lambda: comms.threads.set_project(name, str(second)))))
                    else:
                        # This is the original scheduled continuation, not a
                        # retry of its accepted first input.
                        assert selected.worktree == str(second)
                        assert selected.turn_lease != changed[0][0]
                    return True

            native.provider.response_gate = ChangeAtOriginalProviderRequest()
            try:
                async with asyncio.timeout(60):
                    response = await owner.prompt(session, [{
                        'type': 'text', 'text': 'One authored native project-change question.'}])
                    assert response.stop_reason == 'end_turn'
                    WakeScheduleCheck(session_id=session, inputs=owner.inputs).schedule()
                    await owner.inputs.wake_tasks[session]
            finally:
                native.provider.response_gate = None
            assert len(changed) == 1 and changed[0][1].changed
            assert native.provider.posts == 2
            assert len(native.saved_inputs()) == 2
            assert 'Project change completed' in native.saved_inputs()[1]['content'][0]['text']
            assert comms.registry.require(name).session_file == str(native.session)
            assert comms.registry.require(name).worktree == str(second)
            assert comms.registry.require(name).turn_lease is None
            assert len(comms.registry.all_threads()) == 1
            # A native input can own several context-observation generations.
            assert len({row['inputId'] for row in read_proof_rows(native.session)}) == 2
            assert updates  # Genuine ACP publications reached the attached client.
            assert (await query())['result']['worktree'] == str(second)
            stale_generation = replace(request, binding=replace(request.binding,
                owner=replace(request.binding.owner, generation=request.binding.owner.generation + 1)))
            assert 'error' in await query(stale_generation)
            stale_process = replace(request, binding=LiveThreadOwnerBinding(request.binding.owner,
                replace(request.binding.process, start_time=request.binding.process.start_time + 1)))
            assert 'error' in await query(stale_process)
            assert 'error' in await query(replace(request, thread='foreign-project'))
            print('original_project_observation_ms=' + json.dumps(durations))
    assert all(child.retired for child in native.children)
