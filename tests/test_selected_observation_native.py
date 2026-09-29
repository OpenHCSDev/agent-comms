"""Actual selected settings observation is read-only; cancelled receipt grants nothing."""

import asyncio
import os
from pathlib import Path

import pytest

from agent_comms.native_pi import NativePiUnavailable
from agent_comms.selected_pi_route import read_selected_compaction_decision

pytest_plugins = ("test_backend_native_lifecycle",)


async def test_actual_selected_observation_retirement_without_input_replay(
    native_backend, monkeypatch
):
    native = native_backend
    assert (await native.run("Seed before selected observation"))[-1].ok
    retained = native.persistent.custody.idle()
    child = retained.child.proc
    model = retained.child.attestation.state.model
    assert model is not None
    before = native.session.read_bytes()

    async def observe():
        return await read_selected_compaction_decision(
            native.persistent,
            session_file=str(native.session),
            expected_package=Path(os.environ["PI_COMPACTION_TEST_PACKAGE"]),
            provider=model.provider,
            model_id=model.id,
            context_window=model.context_window,
        )

    decision = await observe()
    assert not decision.enabled
    assert native.persistent.custody.idle() is retained
    assert native.provider.posts == 1
    assert native.session.read_bytes() == before

    # Only the receipt delivery is held. The actual native request/response traverses stdio.
    reader = retained.child.reader
    read = reader.readline
    received = asyncio.Event()
    release = asyncio.Event()

    async def held_receipt(**options):
        raw = await read(**options)
        received.set()
        await release.wait()
        return raw

    monkeypatch.setattr(reader, "readline", held_receipt)
    attempt = asyncio.create_task(observe())
    try:
        await asyncio.wait_for(received.wait(), 5)
        attempt.cancel()
        with pytest.raises(asyncio.CancelledError):
            await attempt
        assert not child.alive()
        assert not native.persistent.available
        assert native.persistent.custody.session_file == str(native.session)
        assert native.provider.posts == 1
        assert native.session.read_bytes() == before
        assert len(native.saved_inputs()) == 1
        assert (await native.run("New explicit input after cancelled observation"))[-1].ok
        assert native.persistent.custody.idle().child.proc is not child
        assert native.provider.posts == 2
        assert [row[2] for row in native.starts] == [
            "Seed before selected observation",
            "New explicit input after cancelled observation",
        ]
        assert len(native.saved_inputs()) == 2

        # Changed custody revision refuses before transmitting any new observation.
        current_child = native.persistent.custody.idle().child.proc
        stat = native.session.stat()
        os.utime(native.session, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000))
        with pytest.raises(NativePiUnavailable):
            await observe()
        assert current_child.alive()
        assert native.provider.posts == 2
        assert len(native.saved_inputs()) == 2
    finally:
        release.set()
        attempt.cancel()
        await asyncio.gather(attempt, return_exceptions=True)
