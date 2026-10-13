"""Strict stored reads are refused in scope without killing the observer.

No provider input, retry or live bus write. The fixture is a canonical saved
manifest whose declaration name is intentionally unavailable in this reader.
"""

import asyncio
import json
import pickle

import pytest

from agent_comms.context_segments.response import SelectedResponseSegment
from agent_comms.field_codec import FieldCodec
from agent_comms.threads import Thread
from agent_comms.ui_model.observation import (
    ObservationProcess,
    ObserveSidebar,
    ObserveThreads,
    ReadNotifications,
    ReadRefused,
    ReadTranscript,
    SidebarUnreadable,
    ThreadsObserved,
    UnreadableHistory,
)
from agent_comms.ui_model.status import OpenThreadStatus
from agent_comms.wire_record import (
    ContextManifestWireObservation,
    ObservationWireRecord,
    WireRecord,
)
from test_observer_death import readable
from test_turn_context_wire import manifest


def saved_row(comms):
    owner = Thread("saved", frozenset(), str(comms.root.parent), created_at=17003.0)
    comms.registry.declare(owner)
    comms.messaging.initialize_private_initial_protocol()
    comms.bus.log.path.touch(mode=0o600)
    row = ObservationWireRecord(ContextManifestWireObservation(manifest(owner))).to_wire()
    return row


def test_recorded_selected_response_remains_a_strict_codec_member(comms):
    row = saved_row(comms)
    segment = row["observation"]["manifest"]["segments"][0]
    segment["kind"] = "selected_response"
    decoded = WireRecord.public_from_wire(row)
    recorded = decoded.context_manifests()[0]
    assert recorded.segments[0].kind is SelectedResponseSegment
    assert FieldCodec.encode(decoded) == row
    # Unknown names still fail, not skipped, substituted or rendered as a
    # guessed current producer. The saved declaration is the real decoder.
    segment["kind"] = "unavailable_future_segment"
    with pytest.raises(ValueError, match="Unknown ContextSegment"):
        WireRecord.public_from_wire(row)


def test_read_failure_and_thread_status_keep_the_explanation_across_the_pipe(comms):
    row = saved_row(comms)
    row["observation"]["manifest"]["segments"][0]["kind"] = "unavailable_future_segment"
    comms.bus.log.path.write_text(json.dumps(row) + "\n")
    answer = ReadTranscript(str(comms.root), "saved", request_id=7).answer(comms)
    assert isinstance(answer, ReadRefused)
    assert isinstance(answer.error, UnreadableHistory)
    assert "unavailable_future_segment" in str(answer.error)
    round_trip = pickle.loads(pickle.dumps(answer))
    assert str(round_trip.error) == str(answer.error)
    observed = ThreadsObserved(None, {}, {"saved": answer.error}, frozenset())
    status = OpenThreadStatus(lambda callback: callback())
    status.observed(observed)
    assert not status.rows.rows["saved"].available
    assert "unavailable_future_segment" in status.rows.rows["saved"].lines[0]


async def test_unknown_saved_member_does_not_kill_the_real_observation_process(comms):
    row = saved_row(comms)
    row["observation"]["manifest"]["segments"][0]["kind"] = "unavailable_future_segment"
    comms.bus.log.path.write_text(json.dumps(row) + "\n")
    original = comms.bus.log.path.read_bytes()
    process = ObservationProcess(comms.root)
    try:
        await readable(process)
        process.results()
        process.request(ObserveSidebar(str(comms.root.parent), True, True))
        async with asyncio.timeout(30):
            while True:
                await readable(process)
                results = process.results()
                failed = next(
                    (result for result in results if isinstance(result, SidebarUnreadable)), None
                )
                if failed is not None:
                    break
        assert "unavailable_future_segment" in str(failed.error)
        assert process.process.is_alive() and process.died is None
        process.request(ObserveThreads(frozenset({"saved"})))
        async with asyncio.timeout(30):
            while True:
                await readable(process)
                results = process.results()
                threads = next(
                    (result for result in results if isinstance(result, ThreadsObserved)), None
                )
                if threads is not None:
                    break
        assert isinstance(threads.unavailable["saved"], UnreadableHistory)
        assert "unavailable_future_segment" in str(threads.unavailable["saved"])
        # Serve a separate, readable request using the same actual pipe/child.
        waiting = asyncio.create_task(process.read(ReadNotifications(str(comms.root), ())))
        await asyncio.sleep(0)
        async with asyncio.timeout(30):
            while not waiting.done():
                await readable(process)
                process.results()
        assert await waiting == {}
        assert process.process.is_alive() and process.died is None
        assert comms.bus.log.path.read_bytes() == original
    finally:
        process.close()
