"""Saved SDK fork selection through original registry/ACP/native resources."""

import asyncio
import os
from functools import partial
from pathlib import Path

import pytest

from acp.agent.router import build_agent_router
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.coordinator import Coordination
from agent_comms.coordination_errors import StaleFence
from agent_comms.input_disposition import InputDispositions
from agent_comms.native_fork import ForkSessionRequest
from agent_comms.native_input_owner import RegistryOwner
from test_backend_native_lifecycle import native_backend


@pytest.mark.skipif(not os.environ.get("PI_COMPACTION_TEST_PACKAGE"),
                    reason="Explicit saved-SDK artifact execution purpose required")
@pytest.mark.parametrize("outcome", ["complete", "cancel", "owner-loss"])
async def test_saved_fork_restores_only_original_idle_owner(native_backend, outcome):
    native = native_backend
    await native.author_history()
    parent_bytes = native.session.read_bytes()
    async with native.open_owner(runtime_enabled=True, auto_wake=False) as (agent, session):
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
        selected_children = []

        async def exercise():
            async with agent.sessions.selected_native_fork(session, original, creation) as selected:
                current = comms.registry.snapshot()
                assert selected.thread.incarnation == original.thread.incarnation
                assert selected.admission_generation == original.admission_generation
                assert current.admission_identity(session) == snapshot.admission_identity(session)
                assert current.owner_identity(session) == snapshot.owner_identity(session)
                assert current.require(session).task_scope == original.thread.task_scope
                assert agent.sessions.require(session) == original.thread.name
                assert not retained_parent.alive()
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
        current = comms.registry.snapshot()
        if outcome == "owner-loss":
            assert current.require(session).session_file == creation.session_file
            assert current.admission_identity(session) != snapshot.admission_identity(session)
        else:
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
