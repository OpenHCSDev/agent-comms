"""Actual selected settings observation is read-only; cancelled receipt grants nothing."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from agent_comms.pi_summary_payloads import SelectedModel
from agent_comms.native_pi import NativePiUnavailable
from agent_comms.selected_pi_route import (
    SelectedPiProbeUnknownError,
    observe_selected_compaction_decision,
)

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
    package = Path(os.environ["PI_COMPACTION_TEST_PACKAGE"])

    async def observe(expected_package=package):
        return await observe_selected_compaction_decision(
            native.persistent,
            session_file=str(native.session),
            expected_package=expected_package,
            selected=SelectedModel(model.provider, model.id, model.context_window),
        )

    decision = await observe()
    assert not decision.enabled
    assert native.persistent.custody.idle() is retained
    assert native.provider.posts == 1
    assert native.session.read_bytes() == before
    with pytest.raises(SelectedPiProbeUnknownError, match="stale"):
        await observe(package / "foreign-package")
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


@pytest.mark.parametrize(
    "path,value",
    [
        (("id",), "uncorrelated-response"),
        (("data", "sessionId"), "foreign-session"),
        (("data", "selected", "provider"), "foreign-provider"),
        (("data", "selected", "contextWindow"), True),
        (("data", "decision", "reserveTokens"), True),
        (("data", "decision", "reason"), "foreign"),
        (("data", "decision", "keepRecentTokens"), 0),
        (("data", "summary"), "unauthorized extra field"),
        ((), None),  # Received from native, but not delivered before the deadline.
    ],
    ids=[
        "correlation",
        "session",
        "model",
        "window",
        "reserve",
        "trigger",
        "keep",
        "extra",
        "timeout",
    ],
)
async def test_actual_selected_observation_untrusted_receipt_retires_without_replay(
    native_backend, monkeypatch, path, value
):
    native = native_backend
    assert (await native.run("Seed before observation transport fault"))[-1].ok
    retained = native.persistent.custody.idle()
    model = retained.child.attestation.state.model
    before = native.session.read_bytes()
    read = retained.child.reader.readline
    receipts = []

    async def damaged_receipt(**options):
        raw = await read(**options)
        receipts.append(raw)
        if not path:
            await asyncio.Event().wait()
        record = json.loads(raw)
        target = record
        for component in path[:-1]:
            target = target[component]
        target[path[-1]] = value
        return json.dumps(record).encode() + b"\n"

    monkeypatch.setattr(retained.child.reader, "readline", damaged_receipt)
    with pytest.raises(SelectedPiProbeUnknownError):
        await observe_selected_compaction_decision(
            native.persistent,
            session_file=str(native.session),
            expected_package=Path(os.environ["PI_COMPACTION_TEST_PACKAGE"]),
            selected=SelectedModel(model.provider, model.id, model.context_window),
            timeout=1,
        )
    assert len(receipts) == 1  # A real pinned-native response, not a fake host.
    assert not retained.child.proc.alive()
    assert not native.persistent.available
    assert native.persistent.custody.session_file == str(native.session)
    assert native.provider.posts == len(native.saved_inputs()) == 1
    assert native.session.read_bytes() == before
