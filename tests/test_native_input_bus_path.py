"""A saved native owner answers a fresh DM through its bus-owned proof resource."""
import asyncio
import json
from pathlib import Path
from contextlib import AsyncExitStack, suppress
from dataclasses import replace
import signal
from time import perf_counter

import agent_comms.native_package as native_package
import pytest

from agent_comms.agent_backend import ClearQueue, InputContent, InputRequest, SteerInputPolicy, TurnInbox
from agent_comms.backend import NativeSession
from agent_comms.child_process import Platform
from agent_comms.field_codec import InputId
from agent_comms.locked_store import RegistryFile
from agent_comms.store_files import RegistryLocking
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


@pytest.fixture
def steering_input() -> InputRequest:
    return InputRequest(
        input_id=InputId.mint(), content=InputContent(text="Second isolated input"),
        when_busy=SteerInputPolicy,
    )


@pytest.mark.parametrize("clear, expected_turns", ((False, 2), (True, 1)))
async def test_written_following_input_settles_after_native_queue_decision(
    tmp_path: Path, native_runtime_package: VerifiedNativePackage,
    clear: bool, expected_turns: int, steering_input: InputRequest,
):
    async with native_backend_fixture(tmp_path, package=native_runtime_package) as native:
        native.provider.response_gate = asyncio.Event()
        inbox = TurnInbox()
        task = asyncio.create_task(native.run("First isolated turn", queue=inbox))
        try:
            await wait_for(lambda: native.provider.posts == 1)
            running = NativeSession.active[task]
            following = steering_input
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


async def test_agent_settled_returns_an_unwritten_inflight_steer_and_reports_not_sent(
    tmp_path: Path, native_runtime_package: VerifiedNativePackage, steering_input: InputRequest,
):
    async with native_backend_fixture(tmp_path, package=native_runtime_package) as native:
        gate = asyncio.Event()
        native.provider.response_gate = gate
        inbox = TurnInbox()
        task = asyncio.create_task(native.run("Finish while the next input awaits ingress", queue=inbox))
        try:
            await wait_for(lambda: native.provider.posts == 1)
            running = NativeSession.active[task]
            # Hold the actual wire lock, not a patched forwarder or send seam.
            async with RegistryLocking.write_guard_async(RegistryFile.path_in(native.root)):
                inbox.put_nowait(steering_input)
                await wait_for(lambda: running.inputs.current is steering_input)
                gate.set()
                await wait_for(lambda: inbox.has_ready_work)
            result = await task
            assert not result[-1].ok
            assert result[-1].reason_code == "queued_input_not_sent"
            assert inbox.get_nowait() is steering_input
            assert native.provider.posts == 1 and len(native.starts) == 1
            assert not native.persistent.child_state.retained
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_busy_snapshot_restarts_forwarding_and_accepts_a_third_input(
    tmp_path: Path, native_runtime_package: VerifiedNativePackage, steering_input: InputRequest,
):
    async with native_backend_fixture(tmp_path, package=native_runtime_package) as native:
        first_gate, second_gate = asyncio.Event(), asyncio.Event()
        native.provider.response_gate = first_gate
        inbox = TurnInbox()
        finish = asyncio.Event()
        task = asyncio.create_task(native.run("First of three real native inputs", queue=inbox,
                                             finish_event=finish))
        try:
            await wait_for(lambda: native.provider.posts == 1)
            running = NativeSession.active[task]
            original_forwarder = running.native.forwarding.tasks[0]
            inbox.put_nowait(steering_input)
            await wait_for(lambda: bool(running.inputs.pending))
            await running.native.reader.pending.before_settlement()
            native.provider.response_gate = second_gate
            first_gate.set()
            await wait_for(lambda: native.provider.posts == 2)
            finish.set()
            await wait_for(lambda: original_forwarder.done() and bool(running.native.forwarding.tasks))
            assert original_forwarder not in running.native.forwarding.tasks
            assert not finish.is_set()
            third = replace(steering_input, input_id=InputId.mint(),
                            content=InputContent(text="Third isolated input after busy settlement"))
            inbox.put_nowait(third)
            await wait_for(lambda: bool(running.inputs.pending))
            await running.native.reader.pending.before_settlement()
            native.provider.response_gate = None
            second_gate.set()
            result = await task
            assert result[-1].ok, result[-1]
            assert native.provider.posts == 3
            assert [started[0] for started in native.starts[1:]] == [steering_input.input_id, third.input_id]
            assert running.inputs.settled and running.stats.complete
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_cancelled_pipe_drain_does_not_requeue_an_already_written_steer(
    tmp_path: Path, native_runtime_package: VerifiedNativePackage, steering_input: InputRequest,
):
    async with native_backend_fixture(tmp_path, package=native_runtime_package) as native:
        gate = asyncio.Event()
        native.provider.response_gate = gate
        inbox = TurnInbox()
        finish = asyncio.Event()
        task = asyncio.create_task(native.run("A real pipe-backed handoff", queue=inbox,
                                             finish_event=finish))
        try:
            await wait_for(lambda: native.provider.posts == 1)
            running = NativeSession.active[task]
            identity = running.native.proc.identity
            original_forwarder = running.native.forwarding.tasks[0]
            platform = Platform.current()
            platform.send(identity, signal.SIGSTOP)
            try:
                following = replace(steering_input, content=InputContent(text="x" * 262144))
                inbox.put_nowait(following)
                await wait_for(lambda: bool(running.inputs.pending))
                assert not running.native.forwarding.tasks[0].done()
                assert not running.inputs.unwritten_inputs
                finish.set()
                await wait_for(original_forwarder.done)
                assert inbox.empty()
                assert all(not entry.future.done() for entry in running.inputs.pending.values())
            finally:
                with suppress(ProcessLookupError):
                    platform.send(identity, signal.SIGCONT)
            await running.native.reader.pending.before_settlement()
            gate.set()
            result = await task
            assert result[-1].ok, result[-1]
            assert native.provider.posts == 2
            assert [started[0] for started in native.starts[1:]] == [following.input_id]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
