"""Idle observations avoid repeated work without becoming delivery authority."""

from bus_history import read_history
from conftest import publish_initial
from agent_comms.messages import NoticeRole
import asyncio
import os
import threading
from contextlib import AsyncExitStack

import pytest

import agent_comms.agent_events as events
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.coordinator import Coordination
from agent_comms.child_process import ProcessIdentity
from agent_comms.owned_turn import OwnedTurn
from agent_comms.messages import AlertKind, InfoKind, Message
from agent_comms.private_path import FileRevision
from agent_comms.store_files import _store_lock
from test_acp_private_nk_delivery import _session
from test_coordinated_runtime import _root, tmp_path  # noqa: F401


@pytest.mark.asyncio
async def test_observation_waits_for_the_held_bus_attachment(tmp_path):
    comms, agent, root_id = _session(tmp_path)
    # A busy original source is pending, not an unavailable inbox. Cancelling
    # one waiter must leave the holder and the other observations intact.
    with _store_lock(comms.bus.bus.path):
        tasks = [asyncio.create_task(agent.inputs._observe_private_revision("beta", root_id))
                 for _ in range(3)]
        await asyncio.sleep(0.08)
        assert not any(task.done() for task in tasks)
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        assert not any(task.done() for task in tasks[1:])
    observed = await asyncio.wait_for(asyncio.gather(*tasks[1:]), 5)
    assert observed[0] == observed[1]


@pytest.mark.asyncio
async def test_empty_delivery_recovery_does_not_wait_for_wire_writer(tmp_path):
    from agent_comms.queued_input import DeliveredInput

    comms, agent, root_id = _covered_session(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def writer():
        with comms.bus.bus.writing():
            entered.set()
            assert release.wait(5)

    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    assert entered.wait(2)
    task = asyncio.create_task(DeliveredInput.recover_interrupted(agent.inputs, "beta", root_id))
    try:
        # The real write lock is held; an empty recovery must finish without it.
        done, _ = await asyncio.wait({task}, timeout=1)
        assert task in done
        assert task.result() == 0
    finally:
        release.set()
        writer_thread.join(5)
        await task


@pytest.mark.asyncio
async def test_turn_publication_joins_cancellation_before_lease_release(tmp_path):
    comms, agent, _root_id = _session(tmp_path)
    origin = Message("user", "#team", "publication ownership", InfoKind)
    execution = OwnedTurn(
        agent.turns, "beta", "beta", "publication ownership", origins=(origin,)
    )
    async with AsyncExitStack() as resources:
        async with AsyncExitStack() as permits:
            assert await execution.acquire(resources, permits)
            progress = execution.progress
            # Only native events are supplied. Registry, original input,
            # wire notices, diagnostic and final checkpoint use real owners.
            await progress.chunk(events.Chunk("committed progress"))
            await progress.committed_progress(events.CommittedProgress("committed progress"))
            assert progress.reply_parts == []
            assert [(row.body, isinstance(row.role, NoticeRole)) for row in read_history(comms)] == [
                ("committed progress", True),
            ]
            await progress.done(events.Done("refused", False))
            # A real writer holds the bus, so the failure notice's worker waits
            # on the flock while the turn is cancelled.
            with _store_lock(comms.bus.bus.path):
                task = asyncio.create_task(progress.publish_result())
                await asyncio.sleep(0.04)
                task.cancel()
                await asyncio.sleep(0.04)
                assert not task.done()
                assert comms.registry.require("beta").turn_lease == execution.turn_lease
            with pytest.raises(asyncio.CancelledError):
                await task
            rows = read_history(comms)
            assert len(rows) == 2 and isinstance(rows[-1].role, NoticeRole)
            assert rows[-1].type is AlertKind
            assert comms.registry.require("beta").turn_lease == execution.turn_lease
    assert not comms.registry.require("beta").executing


def _covered_session(tmp_path):
    comms, agent, root_id = _session(tmp_path)
    other = comms.registry.new_thread('alpha', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    comms.registry.declare(other)
    with Coordination(comms.root / "coordination.sqlite3") as store:
        store.participants.register(
            RecordedThreadIncarnation("fixture", other.created_at).lookup, other.name, other.name, committed=True,
        )
    publish_initial(comms, "sender", other.name, "another recipient's original source")
    return comms, agent, root_id


def test_coordination_read_preserves_revision_and_repairs_exposed_mode(tmp_path):
    path = tmp_path / "coordination.sqlite3"
    with Coordination(path):
        pass
    revision = FileRevision.capture(path)
    with Coordination(path):
        pass
    assert FileRevision.capture(path) == revision
    path.chmod(0o644)
    with Coordination(path):
        assert path.stat().st_mode & 0o777 == 0o600


    # This control observes actual delivery/recovery facts. It neither mocks
    # native execution nor grants a provider call to the pending input.


