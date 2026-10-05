"""Saved SDK fork selection through original registry/ACP/native resources."""

import asyncio
import os
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

from acp.agent.router import build_agent_router
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.coordinator import Coordination
from agent_comms.coordination_errors import StaleFence
from agent_comms.input_disposition import InputDispositions
from agent_comms.native_fork import ForkSessionRequest
from agent_comms.native_input_owner import RegistryOwner
from agent_comms.native_pi import NativePiUnavailable
from test_backend_native_lifecycle import native_backend


@pytest.mark.skipif(not os.environ.get("PI_COMPACTION_TEST_PACKAGE"),
                    reason="Explicit saved-SDK artifact execution purpose required")
@pytest.mark.parametrize("outcome", ["complete", "cancel", "owner-loss"])
async def test_saved_fork_restores_only_original_idle_owner(native_backend, outcome):
    native = native_backend
    await native.author_history()
    parent_bytes = native.session.read_bytes()
    selected_children = []
    competing_reads = []

    async def observe_restoration(*, session_id, update):
        if not selected_children or competing_reads:
            return
        snapshot = await Coordination.run_worker(agent._comms.registry.snapshot)
        thread = snapshot.require(session_id)
        if thread.session_file != str(native.session):
            return
        persistent = agent.turns.persistent_backends[session_id]
        assert agent.turns.turn_locks[session_id].locked()
        assert persistent.lock.locked()
        assert persistent.custody.identity.session_file == str(native.session)
        assert not selected_children[-1].alive()
        request = asyncio.create_task(agent.turns.inspect_context(session_id, thread))
        competing_reads.append(request)
        await asyncio.sleep(0)
        assert persistent.lock.locked() and not request.done()

    async with native.open_owner(runtime_enabled=True, auto_wake=False,
                                 client=SimpleNamespace(session_update=observe_restoration)) as (agent, session):
        comms = agent._comms
        snapshot = await Coordination.run_worker(comms.registry.snapshot)
        original = RegistryOwner.capture_local(snapshot, session)
        retained_parent = agent.turns.persistent_backends[session].custody.child.proc
        journal = await Coordination.run_worker(partial(
            CompactionJournal, comms.root / "compaction-commits.sqlite3",
        ))
        creation = await journal.private_inputs.fork(ForkSessionRequest(
            os.environ["PI_COMPACTION_TEST_PACKAGE"], str(native.session), str(native.project),
            directory=str(native.session.parent),
        ), cwd=native.project)
        child_bytes = Path(creation.session_file).read_bytes()

        async def exercise():
            async with agent.sessions.selected_native_fork(session, original, creation) as (selected, retire_selected):
                current = comms.registry.snapshot()
                assert selected.thread.incarnation == original.thread.incarnation
                assert selected.admission_generation == original.admission_generation
                assert current.admission_identity(session) == snapshot.admission_identity(session)
                assert current.owner_identity(session) == snapshot.owner_identity(session)
                assert current.require(session).task_scope == original.thread.task_scope
                assert agent.sessions.require(session) == original.thread.name
                assert not retained_parent.alive()
                try:
                    await agent.turns.prepare_selected_session(session, selected.thread)
                    selected_children.append(agent.turns.persistent_backends[session].custody.child.proc)
                    if outcome == "complete":
                        response = await build_agent_router(agent)("session/prompt", {
                            "sessionId": session,
                            "prompt": [{"type": "text", "text": "One distinct same-owner fork input."}],
                        }, False)
                        assert response.stop_reason == "end_turn"
                        document = agent.inputs.dispositions.read()
                        row, = document.rows.values()
                        assert row.has_started
                        assert not agent.turns.turn_tasks and not agent.inputs.backend_inboxes
                        assert Path(creation.session_file).read_bytes().startswith(child_bytes)
                        assert native.provider.posts == 1
                    elif outcome == "cancel":
                        asyncio.current_task().cancel()
                        await asyncio.sleep(0)
                    else:
                        current = comms.registry.require(session)
                        await Coordination.run_worker(partial(comms.registry.fence_idle_owners, (
                            (current, original.admission_generation),
                        )))
                finally:
                    await retire_selected()

        if outcome == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await exercise()
        elif outcome == "owner-loss":
            with pytest.raises(StaleFence, match="Idle native source owner changed"):
                await exercise()
        else:
            await exercise()

        assert native.session.read_bytes() == parent_bytes
        assert all(not child.alive() for child in selected_children)
        contexts = await asyncio.gather(*competing_reads)
        current = comms.registry.snapshot()
        if outcome == "owner-loss":
            assert not contexts
            assert current.require(session).session_file == creation.session_file
            assert current.admission_identity(session) != snapshot.admission_identity(session)
        else:
            context, = contexts
            assert context.identity.session_file == str(native.session)
            restored = current.require(session)
            assert restored.session_file == original.thread.session_file
            assert restored.incarnation == original.thread.incarnation
            assert restored.active_turn is None
            assert current.admission_identity(session) == snapshot.admission_identity(session)
            assert current.owner_identity(session) == snapshot.owner_identity(session)
            retained = agent.turns.persistent_backends[session].custody
            assert retained.identity.session_file == original.thread.session_file
            assert retained.child.proc.alive()
        if outcome != "complete":
            assert native.provider.posts == 0
            assert not InputDispositions(comms.root / InputDispositions.filename).read().rows
            assert Path(creation.session_file).read_bytes() == child_bytes
    assert all(child.retired for child in native.children)


@pytest.mark.skipif(not os.environ.get("PI_COMPACTION_TEST_PACKAGE"),
                    reason="Explicit saved-SDK artifact execution purpose required")
@pytest.mark.parametrize("boundary", ["entry", "exit"])
@pytest.mark.parametrize("replacement", ["owner", "configuration"])
async def test_saved_fork_refuses_before_retiring_successor(native_backend, boundary, replacement):
    native = native_backend
    await native.author_history()
    successor_children = []
    async with native.open_owner(runtime_enabled=True, auto_wake=False) as (agent, session):
        comms = agent._comms
        original_snapshot = await Coordination.run_worker(comms.registry.snapshot)
        original = RegistryOwner.capture_local(original_snapshot, session)
        journal = await Coordination.run_worker(partial(
            CompactionJournal, comms.root / "compaction-commits.sqlite3"))
        creation = await journal.private_inputs.fork(ForkSessionRequest(
            os.environ["PI_COMPACTION_TEST_PACKAGE"], str(native.session), str(native.project),
            directory=str(native.session.parent)), cwd=native.project)

        async def replace_owned_child():
            previous = agent.turns.persistent_backends[session].custody.child.proc

            def acquire():
                if replacement == "owner":
                    snapshot = comms.registry.snapshot()
                    current = snapshot.require(session)
                    comms.registry.fence_idle_owners(((
                        current, snapshot.admission_generations[session]),))
                    # Original heartbeat owns STOPPED -> RUNNING admission and
                    # owner generations, even when this PID/incarnation survive.
                    comms.threads.heartbeat(session)
                    return comms.owners.acquire_thread(session, owner_pid=os.getpid())
                return comms.threads.set_thread_thinking_level(session, "low")

            successor = await Coordination.run_worker(acquire)
            await agent.turns.prepare_selected_session(session, successor)
            custody = agent.turns.persistent_backends[session].custody
            successor_children.append(custody.child.proc)
            assert not previous.alive()
            assert custody.child.proc.alive()
            return await Coordination.run_worker(comms.registry.snapshot), custody

        if boundary == "entry":
            successor_snapshot, successor_custody = await replace_owned_child()
            original_bytes = native.session.read_bytes()
            with pytest.raises(StaleFence, match="Idle native source owner changed"):
                async with agent.sessions.selected_native_fork(session, original, creation):
                    pytest.fail("Stale source selection must not enter its body")
        else:
            original_bytes = native.session.read_bytes()
            with pytest.raises(NativePiUnavailable, match="another original owner binding|another selected configuration"):
                async with agent.sessions.selected_native_fork(session, original, creation) as (selected, retire_selected):
                    await agent.turns.prepare_selected_session(session, selected.thread)
                    successor_snapshot, successor_custody = await replace_owned_child()
                    try:
                        assert comms.registry.require(session).session_file == creation.session_file
                    finally:
                        await retire_selected()

        assert agent.turns.persistent_backends[session].custody is successor_custody
        assert successor_custody.child.proc.alive()
        current = await Coordination.run_worker(comms.registry.snapshot)
        assert current.require(session) == successor_snapshot.require(session)
        assert current.owner_identity(session) == successor_snapshot.owner_identity(session)
        assert current.admission_identity(session) == successor_snapshot.admission_identity(session)
        assert native.session.read_bytes() == original_bytes
        assert native.provider.posts == 0
        assert not agent.inputs.dispositions.read().rows
    assert all(not child.alive() for child in successor_children)
    assert all(child.retired for child in native.children)
