"""An actual idle owner restart preserves the saved session and retained state."""

import asyncio

from agent_comms.agent_events import AgentInfo, ContextUsageObservation
from agent_comms.comms import wire
from agent_comms.launch_root import LaunchRoot
from agent_comms.pi_counts import PiCount
from agent_comms.runtime import socket_path
from agent_comms.native_arguments import GivenBinary
from agent_comms.restart_scope import OwnersNamed
from agent_comms.thread_model import DeclaredModel
from agent_comms.thread_text import SavedSessionFile
from native_backend_fixture import native_backend_fixture


async def test_real_idle_owner_is_replaced_without_losing_session(
        tmp_path, monkeypatch, native_runtime_package, source_programs):
    async with native_backend_fixture(tmp_path, package=native_runtime_package) as native:
        source_programs.bind(native.project)
        monkeypatch.setenv('AGENT_COMMS_AGENT_MODELS', 'response-local/fixture')
        comms = wire(native.root)
        with LaunchRoot.from_path(comms.root).selected():
            comms.registry.declare(comms.registry.new_thread(
                'worker', frozenset(), str(native.project), session_file=SavedSessionFile(str(native.session))))
            comms.agents.observe_runtime_info('worker', ContextUsageObservation(
                context_used=PiCount.from_wire(23)))
            comms.agents.observe_runtime_info('worker', AgentInfo(
                model=DeclaredModel('response-local/fixture'), context_size=PiCount.from_wire(100)))
            comms.ledger.merge({'saved': {'worker': ['work remains', 23]}}, 'worker')
            metadata = comms.agents.runtime_info.path.read_bytes()
            collaboration = comms.ledger.path.read_bytes()
            owner = await asyncio.to_thread(comms.owners.ensure_owner, 'worker', agent_bin='pi')
            process = owner.require_process()

            async def ready(pid):
                async with asyncio.timeout(10):
                    while not socket_path(comms.root, pid).exists():
                        assert comms.registry.require('worker').process_alive
                        await asyncio.sleep(.05)

            try:
                await ready(process.pid)
                results = await asyncio.to_thread(
                    comms.owners.restart_owners, OwnersNamed(('worker',)), agent_bin=GivenBinary('pi'))
                assert results[0].previous_pid == process.pid
                assert results[0].pid != process.pid
                assert not owner.process_alive
                await ready(results[0].pid)
                assert comms.registry.require('worker').session_file == SavedSessionFile(str(native.session))
                assert not comms.registry.require('worker').turn_record.read().executing
                assert comms.agents.runtime_info.path.read_bytes() == metadata
                assert comms.ledger.path.read_bytes() == collaboration
                reopened = wire(comms.root)
                assert reopened.agents.agent_info_of('worker').context_used == PiCount.from_wire(23)
                assert reopened.ledger.read().entries['saved'] == {'worker': ['work remains', 23]}
                assert native.provider.posts == 0
            finally:
                await asyncio.to_thread(comms.owners.stop, 'worker')
