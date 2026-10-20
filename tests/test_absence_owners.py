"""Behaviour-bearing absence belongs to the state that owns the operation."""

import asyncio

import pytest

from agent_comms.agent_backend import InputId, SendNow, TurnInbox
from agent_comms.backend import EventFinishSignal, NoFinishSignal, NoInputStartListener, ReportInputStart
from agent_comms.input_drain import DrainedSession, NoPrivateObservation
from agent_comms.native_child import NoInputForwarder
from agent_comms.native_startup import MeasuredSessionLoad, SessionLoad, UnmeasuredSessionLoad
from agent_comms.turn_input_source import SelectedOriginalInput


def test_drain_lifecycle_keeps_inbox_and_original_in_one_owner():
    state = DrainedSession()
    assert not state.turn_open() and not state.turn_started()
    with pytest.raises(KeyError, match="running turn inbox"):
        state.require_inbox()
    state.to_turn(SendNow((InputId("before-open"),)))
    inbox = state.open_inbox()
    assert state.open_inbox() is inbox and inbox.empty()
    assert state.turn_open() and not state.turn_started()
    with pytest.raises(KeyError, match="started first input"):
        state.require_first_source()
    original = SelectedOriginalInput.answering("original input")
    state.capture_first_source(original)
    assert state.turn_started() and state.require_first_source() is original
    item = SendNow((InputId("live"),))
    state.to_turn(item)
    assert inbox.get_nowait() is item
    # A duplicate/stale cleanup cannot retire the next turn's inbox or source.
    assert not state.retire_turn(TurnInbox()).is_open
    assert state.require_inbox() is inbox and state.require_first_source() is original
    retired = state.retire_turn(inbox)
    assert retired.require_source() is original
    assert not state.turn_open() and not state.turn_started()
    assert state.open_inbox() is not inbox


def test_absent_holder_and_attempt_preserve_refusals_without_runtime_probes():
    from agent_comms.coordination_errors import RecoveryBlocked, StaleFence
    from agent_comms.coordination_tables.attempts import NoPreviousAttempt
    from agent_comms.todos import NoTodoHolder, TodoError

    holder = NoTodoHolder()
    assert holder.holder_name() is None and holder.current_assignments() == ()
    holder.require_trace("first owner", None)
    with pytest.raises(TodoError, match="no previous holder"):
        holder.previous_assignment()
    attempt = NoPreviousAttempt()
    attempt.require_newer_owner(1)
    with pytest.raises(StaleFence, match="no authorized attempt"):
        attempt.require()
    with pytest.raises(RecoveryBlocked, match="unstarted deferral"):
        attempt.require_retry(None)


async def test_native_absence_does_not_admit_a_wait_or_a_forwarding_job():
    assert NoFinishSignal().waiters(settling=False) == ()
    event = asyncio.Event()
    finish = EventFinishSignal(event)
    assert finish.waiters(settling=True) == ()
    (waiter,) = finish.waiters(settling=False)
    event.set()
    assert await waiter
    listener = NoInputStartListener()
    assert listener(None, "native", "input") and listener.started_events == ()
    seen = []
    reporting = ReportInputStart(lambda *args: seen.append(args) or False)
    assert not reporting(None, "native", "input")
    assert seen == [(None, "native", "input")] and len(reporting.started_events) == 1
    calls = []
    async def forward():
        calls.append("start")
        await asyncio.Event().wait()
    absent = NoInputForwarder()
    assert absent.tasks == ()
    await absent.finish()
    running = absent.start(forward)
    assert running.start(forward) is running
    await asyncio.sleep(0)
    assert calls == ["start"]
    await running.finish()
    assert all(task.done() for task in running.tasks)


async def test_unobserved_private_state_and_unmeasured_session_have_no_authority(tmp_path):
    assert not await NoPrivateObservation().coordination_unchanged(None, "session", None)
    assert not DrainedSession().settled()
    missing = SessionLoad.capture(str(tmp_path / "missing.jsonl"))
    assert isinstance(missing, UnmeasuredSessionLoad)
    assert missing.diagnostic == {} and missing.summary == "unknown"
    saved = tmp_path / "saved.jsonl"
    saved.write_bytes(b"saved session")
    measured = SessionLoad.capture(str(saved))
    assert isinstance(measured, MeasuredSessionLoad)
    assert measured.diagnostic == {"session_bytes": len(b"saved session")}
