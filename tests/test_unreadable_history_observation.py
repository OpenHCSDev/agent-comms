"""Strict stored reads are refused in scope without killing the observer.

No provider input, retry or live bus write. The fixture is a canonical saved
manifest whose declaration name is intentionally unavailable in this reader.
"""

import asyncio
import json
import pickle
from dataclasses import dataclass

import pytest

from agent_comms.context_segments.response import SelectedResponseSegment
from agent_comms.coordination_errors import (
    CoordinationReadUnavailable,
    SchemaVersionError,
    StaleRevision,
)
from agent_comms.field_codec import FieldCodec
from agent_comms.threads import Thread
from agent_comms.ui_model.observation import (
    ObservationProcess,
    ObserveSidebar,
    ObserveThreads,
    OwnerRead,
    ReadNotifications,
    ReadBusy,
    ReadRefused,
    ReadRequest,
    ReadTranscript,
    SidebarUnreadable,
    ThreadsObserved,
    UnreadableHistory,
)
from agent_comms.ui_model.status import OpenThreadStatus
from agent_comms.turn_context import RecordedContextManifest
from agent_comms.wire_record import (
    ObservationWireRecord,
    RecordedContextManifestWireObservation,
    WireRecord,
)
from test_observer_death import readable
from test_turn_context_wire import manifest


def saved_row(comms):
    owner = Thread("saved", frozenset(), str(comms.root.parent), created_at=17003.0)
    comms.registry.declare(owner)
    comms.messaging.initialize_private_initial_protocol()
    comms.bus.log.path.touch(mode=0o600)
    recorded, _entries = RecordedContextManifest.capture(manifest(owner))
    row = ObservationWireRecord(RecordedContextManifestWireObservation(recorded)).to_wire()
    return row


def test_recorded_selected_response_remains_a_strict_codec_member(comms):
    row = saved_row(comms)
    segment = row["observation"]["manifest"]["segments"][0]["manifest"]
    segment["kind"] = "selected_response"
    decoded = WireRecord.public_from_wire(row)
    recorded = decoded.recorded_context_manifests()[0]
    assert recorded.segments[0].manifest.kind is SelectedResponseSegment
    assert FieldCodec.encode(decoded) == row
    # Unknown names still fail, not skipped, substituted or rendered as a
    # guessed current producer. The saved declaration is the real decoder.
    segment["kind"] = "unavailable_future_segment"
    with pytest.raises(ValueError, match="Unknown ContextSegment"):
        WireRecord.public_from_wire(row)


def test_read_failure_and_thread_status_keep_the_explanation_across_the_pipe(comms):
    row = saved_row(comms)
    row["observation"]["manifest"]["segments"][0]["manifest"]["kind"] = "unavailable_future_segment"
    comms.bus.log.path.write_text(json.dumps(row) + "\n")
    answer = ReadTranscript(str(comms.root), "saved", request_id=7).answer(comms)
    assert isinstance(answer, ReadRefused)
    assert isinstance(answer.error, UnreadableHistory)
    assert "unavailable_future_segment" in str(answer.error)
    round_trip = pickle.loads(pickle.dumps(answer))
    assert str(round_trip.error) == str(answer.error)
    assert UnreadableHistory.capture(answer.error) is answer.error
    observed = ThreadsObserved(None, {}, {"saved": answer.error}, frozenset(), frozenset({"saved"}))
    status = OpenThreadStatus(lambda callback: callback())
    status.observed(observed)
    assert not status.rows.rows["saved"].available
    assert "unavailable_future_segment" in status.rows.rows["saved"].lines[0]


@pytest.mark.parametrize(
    "error_type", [SchemaVersionError, CoordinationReadUnavailable, StaleRevision]
)
def test_store_read_only_retains_the_named_busy_state(comms, error_type):
    @dataclass(frozen=True)
    class FailingRead(ReadRequest):
        error: Exception

        def read(self, comms):
            raise self.error

    error = error_type("original store read explanation")
    if error_type is SchemaVersionError:
        with pytest.raises(error_type, match="original store read explanation"):
            FailingRead(str(comms.root), error).answer(comms)
        return
    answer = FailingRead(str(comms.root), error).answer(comms)
    if error_type is CoordinationReadUnavailable:
        assert isinstance(answer, ReadBusy)
        assert answer.title == "History loading"
        assert pickle.loads(pickle.dumps(answer)) == answer
    else:
        assert isinstance(answer, ReadRefused)
        assert answer.error is error


@pytest.mark.parametrize(
    "error_type", [SchemaVersionError, CoordinationReadUnavailable, StaleRevision]
)
async def test_owner_reply_preserves_its_own_failure_type(comms, error_type):
    @dataclass(frozen=True)
    class FailingOwnerRead(OwnerRead):
        async def read(self, owner):
            raise NotImplementedError

        async def ask(self, comms):
            raise error_type("original owner read explanation")

    saved_row(comms)
    answer = await FailingOwnerRead(
        str(comms.root), comms.registry.require("saved").incarnation
    ).answer(comms)
    assert isinstance(answer, ReadRefused)
    assert isinstance(answer.error, error_type)
    assert not isinstance(answer.error, UnreadableHistory)


@pytest.mark.parametrize("error_type", [CoordinationReadUnavailable, StaleRevision])
def test_sidebar_deferred_read_does_not_replace_its_original_interest(
    comms, monkeypatch, error_type
):
    def deferred(*args, **kwargs):
        raise error_type("original sidebar read deferred")

    monkeypatch.setattr(comms.views, "viewer_snapshot", deferred)
    request = ObserveSidebar(str(comms.root.parent), True, True)
    assert request.answer(comms, comms.views.revision()) is None


async def test_unknown_saved_member_does_not_kill_the_real_observation_process(comms):
    row = saved_row(comms)
    row["observation"]["manifest"]["segments"][0]["manifest"]["kind"] = "unavailable_future_segment"
    comms.bus.log.path.write_text(json.dumps(row) + "\n")
    original = comms.bus.log.path.read_bytes()
    process = ObservationProcess(comms.route)
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
                # The pipe resolved the Future; let its awaiting Task resume.
                # An unchanged error no longer emits an unrelated extra wake.
                await asyncio.sleep(0)
        assert await waiting == {}
        assert process.process.is_alive() and process.died is None
        assert comms.bus.log.path.read_bytes() == original
    finally:
        process.close()
