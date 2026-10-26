"""A saved native owner answers a fresh DM through its bus-owned proof resource."""
import asyncio
import json
from pathlib import Path
from contextlib import AsyncExitStack
from time import perf_counter

import agent_comms.native_package as native_package
import pytest

from agent_comms.agent_backend import ClearQueue, InputContent, InputRequest, SteerInputPolicy, TurnInbox
from agent_comms.backend import NativeSession
from agent_comms.field_codec import InputId
from agent_comms.native_package import VerifiedNativePackage
from agent_comms.turn_watchdog import ModelWait
from agent_comms.native_arguments import NativeInputProofArgument
from agent_comms.native_pi import NativeContextJournal, NativeContextProof
from agent_comms.native_transcript import NativeTranscript
from bus_history import read_history
from delivery_owner_fixture import reply_turn_finished
from native_backend_fixture import native_backend_fixture, wait_for
from native_package_acquisition_fixture import PackageTrace


async def test_saved_owner_dm_replies_and_core_reads_exact_native_proof(tmp_path, native_runtime_package):
    async with native_backend_fixture(tmp_path, package=native_runtime_package) as native:
        await native.author_history()
        trace = PackageTrace(native_package.package_tree_digest.__code__)
        async with AsyncExitStack() as lifetime:
            lifetime.enter_context(trace.active())
            started = perf_counter()
            agent, session = await lifetime.enter_async_context(
                native.open_owner(runtime_enabled=True, auto_wake=True))
            ready_seconds = perf_counter() - started
            comms = agent._comms
            comms.threads.claim_thread('sender', tags=frozenset(), worktree=str(native.project))
            native.provider.text = 'The bus-owned native proof answered.'
            sent = comms.messaging.send('sender', session, 'Qualify this fresh bus-owned proof turn.').message
            async with asyncio.timeout(60):
                await agent.inputs.drain_inbox(session)
                await reply_turn_finished(agent, session)
            assert any(message.body == 'The bus-owned native proof answered.'
                       for message in read_history(comms))
            assert native.provider.posts == 1
            assert 'Original saved question' in json.dumps(native.provider.requests)
            child = agent.turns.persistent_backends[session].child_state.child
            launch = child.key[0]
            files = launch.session.files(launch.root)
            assert NativeInputProofArgument(str(files.input_proof)).argv in tuple(zip(launch.argv, launch.argv[1:]))
            assert files.input_proof.is_relative_to(native.root)
            assert not native.session.with_name(native.session.name+'.input-proof').exists()
            with NativeContextJournal.open_evidence(files) as db:
                rows = NativeContextJournal.select(db)
            assert len(rows) == 1
            record = next(record for record in NativeTranscript(native.session).forward(
                0, native.session.stat().st_size) if record.entry.input_id == rows[0].input_id)
            proof = NativeContextProof.read_evidence(files, rows[0].input_id,
                                                     session_entry_start=record.start)
            assert proof.session_id == files.session_id
            assert not agent.turns.turn_state(session).active.phase.busy
            print('SAVED_OWNER_DM_REPLY_SCOPED_PROOF_AND_NATIVE_HANDOFF=PASS', sent.seq, files.input_proof)
        assert trace.tree_hash_calls == 0
        print('NATIVE_OWNER_READY_SECONDS', ready_seconds, 'TREE_HASH_CALLS', trace.tree_hash_calls,
              'CORE_PACKAGE', native_package.__file__)
    assert all(child.retired for child in native.children)


@pytest.mark.parametrize("clear, expected_turns", ((False, 2), (True, 1)))
async def test_written_following_input_settles_after_native_queue_decision(
    tmp_path: Path, native_runtime_package: VerifiedNativePackage,
    clear: bool, expected_turns: int,
):
    async with native_backend_fixture(tmp_path, package=native_runtime_package) as native:
        native.provider.response_gate = asyncio.Event()
        inbox = TurnInbox()
        task = asyncio.create_task(native.run("First isolated turn", queue=inbox))
        try:
            await wait_for(lambda: native.provider.posts == 1)
            running = NativeSession.active[task]
            following = InputRequest(input_id=InputId.mint(), content=InputContent(text="Second isolated input"),
                                     when_busy=SteerInputPolicy)
            inbox.put_nowait(following)
            await wait_for(lambda: bool(running.inputs.pending))
            await running.native.reader.pending.before_settlement()
            if clear:
                inbox.put_nowait(ClearQueue((following.input_id,)))
                await wait_for(lambda: not running.inputs.pending)
            native.provider.response_gate.set()
            result = await task
            assert result[-1].ok, result[-1]
            assert native.provider.posts == expected_turns
            assert len(native.starts) == expected_turns
            assert [started[0] for started in native.starts[1:]] == (
                [following.input_id] if not clear else []
            )
            assert running.inputs.settled and running.stats.complete
            assert native.persistent.child_state.retained
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_stalled_real_native_run_records_phase_failure_instead_of_name_error(
    tmp_path: Path, native_runtime_package: VerifiedNativePackage,
):
    async with native_backend_fixture(tmp_path, package=native_runtime_package) as native:
        native.provider.status = 0
        result = await native.run("An isolated request whose provider never answers", model_wait=ModelWait(1.0))
        assert not result[-1].ok
        assert result[-1].reason_code == "model_stalled"
        assert native.provider.posts == 1
        assert len(native.starts) == 1
