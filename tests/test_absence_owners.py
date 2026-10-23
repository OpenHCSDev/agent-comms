"""Behaviour-bearing absence belongs to the state that owns the operation."""

from agent_comms.pi_payloads import AnonymousRequest, RequestId
import asyncio

import pytest

from agent_comms.agent_backend import InputId, SendNow, TurnInbox
from agent_comms.backend import EventFinishSignal, FinishSignal, InputStartListener, NoFinishSignal, NoInputStartListener
from agent_comms.change_report import FileLine, NoSourceReference
from agent_comms.field_codec import FieldCodec
from agent_comms.input_drain import DrainedSession
from agent_comms.native_child import NoInputForwarder
from agent_comms.native_startup import MeasuredSessionLoad, SessionLoad, UnmeasuredSessionLoad
from agent_comms.native_session_reopen import SessionRetention
from agent_comms.pi_commands import GetState
from agent_comms.turn_input_source import SelectedOriginalInput
from agent_comms.turn_goal_permission import InactiveGoalPermission, TurnGoalPermission


def test_drain_lifecycle_keeps_inbox_and_original_in_one_owner():
    state = DrainedSession()
    assert not state.turn_open() and not state.turn_started()
    with pytest.raises(KeyError):
        state.require_inbox()
    state.to_turn(SendNow((InputId("before-open"),)))
    inbox = state.open_inbox()
    assert state.open_inbox() is inbox and inbox.empty()
    assert state.turn_open() and not state.turn_started()
    with pytest.raises(KeyError):
        state.require_first_source()
    original = SelectedOriginalInput.answering(
        "original input", TurnGoalPermission(InactiveGoalPermission()))
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
    assert holder.current_assignments() == ()
    holder.require_trace("first owner", NoSourceReference())
    with pytest.raises(TodoError):
        holder.previous_assignment()
    attempt = NoPreviousAttempt()
    attempt.require_newer_owner(1)
    with pytest.raises(StaleFence):
        attempt.require_attempt()
    with pytest.raises(RecoveryBlocked):
        attempt.require_retry(None)


async def test_native_absence_does_not_admit_a_wait_or_a_forwarding_job():
    assert isinstance(FinishSignal.from_event(None), NoFinishSignal)
    assert NoFinishSignal().waiters(settling=False) == ()
    event = asyncio.Event()
    finish = EventFinishSignal(event)
    assert finish.waiters(settling=True) == ()
    (waiter,) = finish.waiters(settling=False)
    event.set()
    assert await waiter
    listener = NoInputStartListener()
    assert listener.original("native", "input") and listener.started_events == ()
    assert listener.following("public", "native", "input")
    seen = []
    reporting = InputStartListener.from_callback(lambda *args: seen.append(args) or False)
    assert not reporting.original("native", "input")
    assert not reporting.following("public", "following-native", "next")
    assert seen == [(None, "native", "input"), ("public", "following-native", "next")]
    assert len(reporting.started_events) == 1
    calls = []
    async def forward():
        calls.append("start")
        await asyncio.Event().wait()
    absent = NoInputForwarder()
    assert absent.tasks == ()
    assert not await absent.finish_if_done(None, ())
    await absent.finish()
    running = absent.start(forward)
    assert running.start(forward) is running
    await asyncio.sleep(0)
    assert calls == ["start"]
    await running.finish()
    assert all(task.done() for task in running.tasks)


async def test_unobserved_private_state_and_unmeasured_session_have_no_authority(tmp_path):
    state = DrainedSession()
    assert not state.settled()
    await state.turn.finish_original(None)
    state.open_inbox()
    await state.turn.finish_original(None)
    assert not SessionRetention.from_optional(None).retain(None, None)
    missing = SessionLoad.capture(str(tmp_path / "missing.jsonl"))
    assert isinstance(missing, UnmeasuredSessionLoad)
    assert missing.diagnostic == {} and missing.summary == "unknown"
    saved = tmp_path / "saved.jsonl"
    saved.write_bytes(b"saved session")
    measured = SessionLoad.capture(str(saved))
    assert isinstance(measured, MeasuredSessionLoad)
    assert measured.diagnostic == {"session_bytes": len(b"saved session")}


def test_source_reference_owns_missing_trace_without_changing_file_line_wire():
    missing = NoSourceReference()
    with pytest.raises(ValueError):
        missing.require("read the failing path")
    assert missing.located() == ()
    location = FileLine("source.py", 17)
    assert location.require("not missing") is location
    assert location.located() == (location,)
    assert FieldCodec.encode(location) == "source.py:17"


def test_optional_rpc_id_is_refused_when_an_input_requires_correlation():
    command = GetState(id=AnonymousRequest())
    assert "id" not in command.to_rpc()
    with pytest.raises(ValueError):
        command.require_id()
    assert GetState(id=RequestId("correlated")).require_id() == "correlated"
