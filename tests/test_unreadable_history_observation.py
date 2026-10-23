"""Strict stored reads are refused in scope without killing the observer.

No provider input, retry or live bus write. The fixture is a canonical saved
manifest whose declaration name is intentionally unavailable in this reader.
"""


from agent_comms.context_store import UNREAD_CONTEXT
import asyncio
import pickle
from dataclasses import dataclass

import pytest

from agent_comms.context_segments.response import SelectedResponseSegment
from agent_comms.coordination_errors import (
    CoordinationReadUnavailable,
    SchemaVersionError,
    StaleRevision,
    TranscriptChanged,
)
from agent_comms.field_codec import FieldCodec
from agent_comms.errors import RelationViolationError, StoredHistoryUnreadable, ToolRefusedError
from agent_comms.runtime import OwnerIdentityChangedError, OwnerReplyLostError
from agent_comms.context_store import ContextRequest
from agent_comms.presentation import WireRevision
from agent_comms.ui_model.observation import (
    ObservationProcess,
    ObserveSidebar,
    ObserveThreads,
    OwnerRead,
    ReadNotifications,
    ReadBusy,
    ReadRefused,
    ReadRequest,
    SidebarObserved,
    SidebarPreparing,
    SidebarReading,
    SidebarReadStage,
    ThreadsObserved,
    ThreadUnavailable,
    UnreadableHistory,
)
from agent_comms.ui_model.status import OpenThreadStatus
from agent_comms.turn_context import RecordedContextManifest
from test_observer_death import readable
from test_turn_context_wire import corrupt_context_kind, manifest
from agent_comms.threads import Thread


@dataclass(frozen=True)
class ReadRecordedContext(ReadRequest):
    thread: str

    def read(self, comms):
        return comms.contexts.page(self.thread, comms.registry)


def saved_row(comms):
    owner = Thread('saved', frozenset(), str(comms.root.parent), created_at=17003.0)
    comms.registry.declare(owner)
    comms.contexts.record_after(UNREAD_CONTEXT, manifest(owner))
    with comms.contexts.snapshot() as db:
        request, = ContextRequest.select(db)
        return FieldCodec.encode(request.recorded(db))


def test_recorded_selected_response_remains_a_strict_codec_member(comms):
    row = saved_row(comms)
    segment = row["segments"][0]["manifest"]
    segment["kind"] = "selected_response"
    recorded = FieldCodec.decode(RecordedContextManifest, row)
    assert recorded.segments[0].manifest.kind is SelectedResponseSegment
    assert FieldCodec.encode(recorded) == row
    # Unknown names still fail, not skipped, substituted or rendered as a
    # guessed current producer. The saved declaration is the real decoder.
    segment["kind"] = "unavailable_future_segment"
    with pytest.raises(ValueError):
        FieldCodec.decode(RecordedContextManifest, row)


def test_read_failure_and_thread_status_keep_the_explanation_across_the_pipe(comms):
    saved_row(comms)
    corrupt_context_kind(comms, "saved", "unavailable_future_segment")
    answer = ReadRecordedContext(str(comms.root), "saved", request_id=7).answer(comms)
    assert isinstance(answer, ReadRefused)
    assert isinstance(answer.error, StoredHistoryUnreadable)
    assert "unavailable_future_segment" in str(answer.error)
    round_trip = pickle.loads(pickle.dumps(answer))
    assert str(round_trip.error) == str(answer.error)
    failure = UnreadableHistory.capture(answer.error)
    assert UnreadableHistory.capture(failure) is failure
    observed = ThreadsObserved(WireRevision.empty(), {"saved": ThreadUnavailable(failure)}, frozenset({"saved"}), {})
    status = OpenThreadStatus(lambda callback: callback())
    status.observed(observed)
    assert status.rows.rows["saved"].status.details == (str(failure),)
    assert "unavailable_future_segment" in status.rows.rows["saved"].lines[0]


@pytest.mark.parametrize(
    "error_type", [SchemaVersionError, CoordinationReadUnavailable, StaleRevision, RelationViolationError]
)
def test_store_read_only_retains_the_named_busy_state(comms, error_type):
    @dataclass(frozen=True)
    class FailingRead(ReadRequest):
        error: Exception

        def read(self, comms):
            raise self.error

    error = error_type("original store read explanation")
    if error_type is SchemaVersionError:
        with pytest.raises(error_type):
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


@pytest.mark.parametrize("error, answered", [
    (StaleRevision("inputs changed during preparation"), ReadBusy),
    (TranscriptChanged(), ReadRefused),
])
def test_a_read_that_defers_staleness_answers_a_reload(comms, error, answered):
    # Retrying bounds that name a replaced source never succeeds; deferring it
    # left an agent tab blank with no error.
    @dataclass(frozen=True)
    class DeferringRead(ReadRequest):
        deferred_reads = UnreadableHistory.DEFERRED_READS

        def read(self, comms):
            raise error

    assert isinstance(DeferringRead(str(comms.root)).answer(comms), answered)


@pytest.mark.parametrize("error_type, refused", [
    (SchemaVersionError, True), (CoordinationReadUnavailable, True), (StaleRevision, True),
    (ToolRefusedError, True), (RelationViolationError, True),
    (ConnectionError, True), (TimeoutError, True),
    (OwnerIdentityChangedError, True), (OwnerReplyLostError, True),
    (RuntimeError, False), (ValueError, False), (TypeError, False),
])
async def test_owner_reply_preserves_its_own_failure_type(comms, error_type, refused):
    @dataclass(frozen=True)
    class FailingOwnerRead(OwnerRead):
        async def read(self, owner):
            raise NotImplementedError

        async def ask(self, comms):
            raise error_type("original owner read explanation")

    saved_row(comms)
    request = FailingOwnerRead(str(comms.root), comms.registry.require("saved").incarnation)
    if not refused:
        with pytest.raises(error_type):
            await request.answer(comms)
        return
    answer = await request.answer(comms)
    assert isinstance(answer, ReadRefused)
    assert isinstance(answer.error, error_type)
    assert not isinstance(answer.error, UnreadableHistory)


def failing(error):
    def read(*args, **kwargs):
        raise error
    return read


DEFERRED, MALFORMED = StaleRevision("source moved during the read"), ValueError("Unknown ContextSegment kind")


@dataclass(frozen=True)
class PreparingStage(SidebarReadStage):
    cause: CoordinationReadUnavailable

    def unavailable(self, *args):
        raise self.cause

    def read(self, request, comms, revision):
        return request._answer(revision, self.unavailable, request.worktree)


@pytest.mark.parametrize("cause", [
    CoordinationReadUnavailable("original sidebar read deferred"),
    StaleRevision("source moved"),
])
def test_sidebar_prepares_once_then_publishes_and_keeps_real_rows(comms, cause):
    request = ObserveSidebar(str(comms.root.parent), True, True)
    revision = comms.views.revision()
    reading = SidebarReading(request)
    reading.stage = stage = PreparingStage(cause)
    first = list(reading.read(comms, revision))
    assert isinstance(first[0], SidebarPreparing)
    assert first[0].cause is cause
    transmitted = pickle.loads(pickle.dumps(first[0]))
    assert type(transmitted.cause) is type(cause)
    assert transmitted.cause.args == cause.args
    assert reading.request is request and reading.stage is stage
    assert not any(isinstance(result, SidebarPreparing) for result in reading.read(comms, revision))
    reading.stage = SidebarReadStage()
    completed = list(reading.read(comms, revision))
    assert len(completed) == 1 and isinstance(completed[0], SidebarObserved)
    assert reading.published is completed[0]
    assert list(reading.read(comms, revision)) == []
    reading.stage = stage
    assert not any(isinstance(result, SidebarPreparing) for result in reading.read(comms, revision))
    assert reading.published is completed[0]


async def test_unknown_saved_member_does_not_kill_the_real_observation_process(comms, durable_home):
    saved_row(comms)
    corrupt_context_kind(comms, "saved", "unavailable_future_segment")
    original = comms.contexts.path.read_bytes()
    original_bus = comms.bus.bus.path.read_bytes()
    process = ObservationProcess(comms.route)
    try:
        await readable(process)
        process.results()
        process.request(ObserveSidebar(str(comms.root.parent), True, True))
        async with asyncio.timeout(30):
            while True:
                await readable(process)
                results = process.results()
                sidebar = next(
                    (result for result in results if isinstance(result, SidebarObserved)), None
                )
                if sidebar is not None:
                    break
        assert process.process.is_alive()
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
        assert not isinstance(threads.results["saved"], ThreadUnavailable)
        # Only the request needing the damaged payload is refused, over the
        # same actual child/pipe that served the sidebar and thread reads.
        damaged = asyncio.create_task(process.read(ReadRecordedContext(str(comms.root), "saved")))
        await asyncio.sleep(0)
        async with asyncio.timeout(30):
            while not damaged.done():
                await readable(process)
                process.results()
                await asyncio.sleep(0)
        with pytest.raises(StoredHistoryUnreadable):
            await damaged
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
        assert process.process.is_alive()
        assert comms.contexts.path.read_bytes() == original
        assert comms.bus.bus.path.read_bytes() == original_bus
    finally:
        process.close()
