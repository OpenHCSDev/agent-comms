"""Actual pinned CLI through ordinary TurnSession; localhost provider only."""

import asyncio
import json
import os
from pathlib import Path

from native_proof_cases import read_proof_rows, write_proof_rows

import pytest

from agent_comms import agent_events as events
from agent_comms import backend


from native_backend_fixture import native_backend_fixture


@pytest.fixture
async def native_backend(tmp_path, monkeypatch):
    if not os.environ.get("PI_COMPACTION_TEST_PACKAGE"):
        pytest.skip("Set PI_COMPACTION_TEST_PACKAGE to the immutable native bundle")
    async with native_backend_fixture(tmp_path) as owner:
        yield owner

async def test_actual_native_queued_settlement_large_reuse_and_validated_reopen(native_backend):
    owner = native_backend
    first = await owner.run("first", followup="queued followup")
    assert isinstance(first[-1], events.Done) and first[-1].ok, first[-1]
    assert [row[2] for row in owner.starts] == ["first", "queued followup"]
    assert [row[0] for row in owner.starts] == [None, "queued"]
    assert len([event for event in first if isinstance(event, events.StreamSettled)]) == 1
    retained = owner.persistent.custody.child.proc
    assert retained is not None and retained.alive()

    await owner.force_reopen()
    assert not retained.alive()
    owner.provider.text = "Reopened retained context."
    reopened = await owner.run("after validated reopen")
    assert reopened[-1].ok and reopened[-1].text == owner.provider.text, reopened[-1]
    assert owner.persistent.custody.child.proc is not retained
    assert owner.persistent.custody.child.proc.alive()
    assert owner.persistent.custody.idle().current
    reopened_child = owner.persistent.custody.child.proc
    # The deliberately oversized output tests framing, not permission to admit
    # a later prompt beyond the model's stored-context budget.
    owner.provider.text = "L" * (2 * 1024 * 1024 + 257)
    second = await owner.run("large response")
    assert second[-1].ok and second[-1].text == owner.provider.text, second[-1]
    assert owner.persistent.custody.child.proc is reopened_child
    assert len(owner.saved_inputs()) == 4
    print(f"ordinary_native_response_bytes={len(second[-1].text.encode())}")

    assert len(owner.starts) == len(owner.saved_inputs()) == owner.provider.posts == 4
    assert len({row[1] for row in owner.starts}) == 4
    proofs = read_proof_rows(owner.session)
    assert {row["inputId"] for row in proofs} == {row[1] for row in owner.starts}


@pytest.mark.parametrize("termination", ["cancel", "eof", "owner_stop"])
async def test_actual_native_interrupted_turn_never_replays_or_retains(native_backend, termination):
    owner = native_backend
    owner.provider.status = 0
    turn = asyncio.create_task(owner.run("single uncertain input"))
    try:
        async with asyncio.timeout(15):
            while owner.provider.posts != 1:
                assert not turn.done(), turn.result() if turn.done() else None
                await asyncio.sleep(0.01)
        child = owner.children[-1]
        session = backend.TurnSession.active[turn]
        stderr, forwarding = session.native.stderr_task, session.steering_task
        if termination == "cancel":
            turn.cancel()
            with pytest.raises(asyncio.CancelledError):
                await turn
        else:
            if termination == "owner_stop":
                await backend.terminate_task_process(turn)
            else:
                await child.stop()
            result = await turn
            assert isinstance(result[-1], events.Done) and not result[-1].ok
        assert turn not in backend.TurnSession.active
        assert stderr.done() and forwarding is not None and forwarding.done()
        assert not child.alive()
        assert not owner.persistent.available
        assert len(owner.starts) == len(owner.saved_inputs()) == owner.provider.posts == 1
    finally:
        if not turn.done():
            turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)


async def test_actual_native_cancelled_owner_stop_keeps_cleanup_custody(
    native_backend, monkeypatch
):
    from agent_comms.native_custody import PiSessionChild, RetiringNative

    owner = native_backend
    owner.provider.status = 0
    turn = asyncio.create_task(owner.run("Uncertain input before interrupted owner stop"))
    entered, release = asyncio.Event(), asyncio.Event()
    close = PiSessionChild.close
    stop = None
    try:
        async with asyncio.timeout(15):
            while owner.provider.posts != 1:
                assert not turn.done()
                await asyncio.sleep(0.01)
        session = backend.TurnSession.active[turn]

        async def held_close(child):
            if child is session.native:
                entered.set()
                await release.wait()
            await close(child)

        monkeypatch.setattr(PiSessionChild, "close", held_close)
        stop = asyncio.create_task(backend.terminate_task_process(turn))
        await asyncio.wait_for(entered.wait(), 5)
        stop.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stop
        assert backend.TurnSession.active[turn] is session
        assert isinstance(owner.persistent.custody, RetiringNative)
        assert not owner.persistent.custody.task.done()
        release.set()
        result = await asyncio.wait_for(turn, 10)
        assert not result[-1].ok
        assert not session.native.proc.alive()
        assert session.native.stderr_task.done() and session.steering_task.done()
        assert turn not in backend.TurnSession.active
        assert owner.provider.posts == len(owner.saved_inputs()) == 1
    finally:
        release.set()
        if not turn.done():
            turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
        if stop is not None:
            await asyncio.gather(stop, return_exceptions=True)
