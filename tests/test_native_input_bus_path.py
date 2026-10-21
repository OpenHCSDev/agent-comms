"""A saved native owner answers a fresh DM through its bus-owned proof resource."""
import asyncio
import json
from contextlib import AsyncExitStack
from time import perf_counter

import agent_comms.native_package as native_package

from agent_comms.native_arguments import NativeInputProofArgument
from agent_comms.native_pi import NativeContextJournal, NativeContextProof
from agent_comms.native_transcript import NativeTranscript
from bus_history import read_history
from delivery_owner_fixture import reply_turn_finished
from native_backend_fixture import native_backend_fixture
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
