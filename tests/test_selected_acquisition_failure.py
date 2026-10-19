"""A selected source fails before send once, through its existing execution store."""
import asyncio

import pytest

from agent_comms.activity import StoppedDrainDiagnostic
from agent_comms.coordination_cohort import pending_sealed_assignments
from agent_comms.coordinator import Coordination
from agent_comms.native_pi import NativePiUnavailable
from agent_comms.selected_session import SelectedSession


async def test_selected_acquisition_failure_settles_before_send(native_backend, monkeypatch):
    native = native_backend
    await native.author_history()
    captured = []
    selected = asyncio.Event()

    async def refuse(cls, participant, path, execution):
        captured.append((participant.lookup, participant.batch.assignment_ids))
        selected.set()
        raise NativePiUnavailable("P632 selected acquisition refused")

    async with native.open_owner(runtime_enabled=True, auto_wake=True) as (agent, session):
        monkeypatch.setattr(SelectedSession, 'prepare', classmethod(refuse))
        comms = agent._comms
        comms.threads.claim_thread('sender', tags=frozenset(), worktree=str(native.project))
        comms.messaging.send('sender', session, 'P632 selected acquisition; one fresh DM')
        await asyncio.wait_for(selected.wait(), 20)
        task = agent.inputs.state(session).wake_task
        assert task is not None
        with pytest.raises(ValueError, match='acquisition refused'):
            await asyncio.wait_for(asyncio.shield(task), 20)
        assert len(captured) == 1
        lookup, assignments = captured[0]
        with Coordination(str(comms.root / 'coordination.sqlite3')) as store:
            for identity in assignments:
                current = store.assignments.get(identity)
                assert not current.lifecycle.engageable
                snapshot = store.snapshots.get(current.lifecycle.execution_id)
                assert snapshot.execution.reason_code == 'session-acquisition-failed'
            assert not pending_sealed_assignments(store, lookup, session, after_seq=0)
        diagnostic = next(view for view in comms.views.thread_views()
                          if view.thread.name == session).activity.readiness.source_diagnostic()
        assert isinstance(diagnostic, StoppedDrainDiagnostic)
        for _ in range(3):
            agent.inputs.wake_observer(session)
        assert task.done() and len(captured) == 1
        assert native.provider.posts == 0
