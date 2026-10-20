"""Actual pinned CLI through ordinary TurnSession; localhost provider only."""

import asyncio
import json
from functools import partial
from pathlib import Path

from native_proof_cases import read_proof_rows, write_proof_rows

import pytest

import agent_comms.agent_events as events
import agent_comms.backend as backend
from agent_comms.acp_extension import InputDeliveryChangedUpdate, QueuePromptRequest, decode_updates, encode_request
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.comms import Comms
from agent_comms.coordinator import Coordination
from agent_comms.input_attempt import ClearedNativeInput
from agent_comms.input_disposition import InputDispositions
from agent_comms.threads import Thread
from stored_inputs import stored_inputs


from native_backend_fixture import wait_for

async def test_actual_native_queued_settlement_large_reuse_and_validated_reopen(native_backend):
    owner = native_backend
    first = await owner.run("first", followup="queued followup")
    assert isinstance(first[-1], events.Done) and first[-1].ok, first[-1]
    assert [row[2] for row in owner.starts] == ["first", "queued followup"]
    assert [row[0] for row in owner.starts] == [None, "queued"]
    assert len([event for event in first if isinstance(event, events.StreamSettled)]) == 1
    retained = owner.persistent.child_state.child.proc
    assert retained is not None and retained.alive()

    await owner.force_reopen()
    assert not retained.alive()
    owner.provider.text = "Reopened retained context."
    reopened = await owner.run("after validated reopen")
    assert reopened[-1].ok and reopened[-1].text == owner.provider.text, reopened[-1]
    assert owner.persistent.child_state.child.proc is not retained
    assert owner.persistent.child_state.child.proc.alive()
    assert owner.persistent.child_state.idle().current
    reopened_child = owner.persistent.child_state.child.proc
    # The deliberately oversized output tests framing and exceeds Core's
    # immutable retained-context limit. Pi compacts it before settling; the
    # history and split-turn prefix each require a summary provider request.
    owner.provider.text = "L" * (2 * 1024 * 1024 + 257)
    second = await owner.run("large response")
    assert second[-1].ok and second[-1].text == owner.provider.text, second[-1]
    assert owner.persistent.child_state.child.proc is reopened_child
    assert len(owner.saved_inputs()) == 4
    print(f"ordinary_native_response_bytes={len(second[-1].text.encode())}")

    assert len(owner.starts) == len(owner.saved_inputs()) == 4
    assert owner.provider.posts == len(owner.provider.requests) == 6
    original_texts = ["first", "queued followup", "after validated reopen", "large response"]
    for request, text in zip(owner.provider.requests[:4], original_texts, strict=True):
        assert request["messages"][-1] == {
            "role": "user", "content": [{"type": "text", "text": text}]
        }
    for request in owner.provider.requests[4:]:
        assert request["messages"][0]["content"].startswith(
            "You are a context summarization assistant."
        )
    compacted = [
        row for row in map(json.loads, owner.session.read_text().splitlines())
        if row["type"] == "compaction"
    ]
    assert len(compacted) == 1
    assert compacted[0]["tokensBefore"] > 300_000
    assert len({row[1] for row in owner.starts}) == 4
    proofs = read_proof_rows(owner.session)
    assert {row["inputId"] for row in proofs} == {row[1] for row in owner.starts}


async def test_actual_native_parked_followup_settles_without_stats_spin(native_backend, monkeypatch):
    from contextlib import contextmanager
    from agent_comms.agent_backend import TurnInbox
    from agent_comms.send_outcome import GrantedSend, HeldSend
    from agent_comms.turn_stats import StatsRequest

    owner = native_backend
    inbox = TurnInbox()
    permit_followup = False
    stats = []
    request = StatsRequest.request

    async def counted_request(self, session):
        if not self.requested:
            stats.append((session.command.message, inbox.ready.is_set()))
        await request(self, session)

    monkeypatch.setattr(StatsRequest, "request", counted_request)

    @contextmanager
    def boundary(public_id, native_id, text):
        yield HeldSend() if public_id == "queued" and not permit_followup else GrantedSend()

    first = await owner.run("finished original", followup="parked followup", queue=inbox,
                            send_boundary=boundary)
    assert first[-1].ok, first[-1]
    assert sum(isinstance(event, events.StreamSettled) for event in first) == 1
    assert stats == [("finished original", False)]  # One terminal query pair; no re-requests.
    assert inbox.qsize() == 1 and not inbox.ready.is_set() and not inbox.has_ready_work
    assert [row[2] for row in owner.starts] == ["finished original"]
    assert len(owner.saved_inputs()) == owner.provider.posts == 1
    retained = owner.persistent.child_state.child.proc
    assert retained.alive()

    # The next distinct turn borrows the same inbox and child. Neither its original
    # nor the parked follow-up may cross the native send seam before READY.
    continuation = asyncio.create_task(owner.run("next original", queue=inbox, send_boundary=boundary))
    try:
        await wait_for(lambda: continuation in backend.TurnSession.active)
        running = backend.TurnSession.active[continuation]
        assert not running.inputs.pending and not inbox.has_ready_work
        assert len(owner.starts) == 1 and len(stats) == 1
        permit_followup = True
        GrantedSend().resume(inbox)
        second = await continuation
        assert second[-1].ok, second[-1]
        assert [row[2] for row in owner.starts] == ["finished original", "next original", "parked followup"]
        assert [row[0] for row in owner.starts] == [None, None, "queued"]
        assert len(owner.saved_inputs()) == owner.provider.posts == 3
        assert len(stats) == 2 and inbox.empty()
        assert owner.persistent.child_state.child.proc is retained
        print(json.dumps({"parked_turn_settled": True, "parked_unstarted": True,
                          "terminal_stats_pairs": 1, "stats_re_requests": 0,
                          "ready_resumed_exactly_once": True, "same_native_child": True}))
    finally:
        continuation.cancel()
        await asyncio.gather(continuation, return_exceptions=True)


async def test_actual_native_failure_after_tool_keeps_terminal_and_effect_once(
    native_backend, monkeypatch
):
    from acp import RequestError
    from acp.schema import TextContentBlock

    owner = native_backend
    await owner.author_history()
    effect = owner.project / "one-effect.txt"
    owner.provider.tool_call = (
        "bash", {"command": "printf 'effect\\n' >> one-effect.txt"}
    )
    chunks = owner.provider.response_chunks

    def fail_after_tool():
        yield from chunks()
        owner.provider.status = 400
        owner.provider.error_message = "controlled failure after actual tool effect"
        owner.provider.error_type = "invalid_request_error"

    monkeypatch.setattr(owner.provider, "response_chunks", fail_after_tool)
    async with owner.open_owner(native_options=("--tools", "bash")) as (agent, session):
        with pytest.raises(RequestError):
            await agent.prompt(session, [TextContentBlock(
                type="text", text="Distinct isolated native terminal verification."
            )])
        assert effect.read_text() == "effect\n"
        journal = [json.loads(line) for line in owner.session.read_text().splitlines()]
        messages = [row["message"] for row in journal if row.get("type") == "message"]
        errors = [message for message in messages if message.get("stopReason") == "error"]
        assert len(errors) == 1 and "controlled failure" in errors[0]["errorMessage"]
        assert owner.provider.posts == 2  # One tool request, one terminal refusal; no retries.
        tools = [message for message in messages if message.get("role") == "toolResult"]
        assert len(tools) == 1
        assert len([message for message in messages if message.get("role") == "user"]) == 2
        reports = list((owner.root / "diagnostics").glob("*.json"))
        failures = [json.loads(path.read_text()) for path in reports]
        assert len(failures) == 1
        assert failures[0]["reason"] == "model_request_failed"


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
        forwarding = session.native.forwarding_task
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
        assert session.native.proc.retired
        await session.native.proc.stderr_tail()
        assert forwarding is not None and forwarding.done()
        assert not child.alive()
        assert not owner.persistent.available
        assert len(owner.starts) == len(owner.saved_inputs()) == owner.provider.posts == 1
    finally:
        if not turn.done():
            turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)


async def test_actual_native_cancelled_owner_stop_keeps_cleanup_resources(
    native_backend, monkeypatch
):
    from agent_comms.native_child import PiSessionChild, RetiringNative

    owner = native_backend
    owner.provider.status = 0
    turn = asyncio.create_task(owner.run("Uncertain input before interrupted owner stop"))
    entered, release = asyncio.Event(), asyncio.Event()
    close = PiSessionChild._shutdown
    stop = None
    try:
        async with asyncio.timeout(15):
            while owner.provider.posts != 1:
                assert not turn.done()
                await asyncio.sleep(0.01)
        session = backend.TurnSession.active[turn]
        forwarding = session.native.forwarding_task

        async def held_close(child, *, graceful):
            if child is session.native:
                entered.set()
                await release.wait()
            return await close(child, graceful=graceful)

        monkeypatch.setattr(PiSessionChild, "_shutdown", held_close)
        stop = asyncio.create_task(backend.terminate_task_process(turn))
        await asyncio.wait_for(entered.wait(), 5)
        stop.cancel()
        assert not stop.done()
        assert backend.TurnSession.active[turn] is session
        assert isinstance(owner.persistent.child_state, RetiringNative)
        assert not owner.persistent.child_state.child._close_job.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await stop
        result = await asyncio.wait_for(turn, 10)
        assert not result[-1].ok
        assert not session.native.proc.alive()
        await session.native.proc.stderr_tail()
        assert forwarding.done()
        assert session.native.forwarding_task is None
        assert turn not in backend.TurnSession.active
        assert owner.provider.posts == len(owner.saved_inputs()) == 1
    finally:
        release.set()
        if not turn.done():
            turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
        if stop is not None:
            await asyncio.gather(stop, return_exceptions=True)


async def test_clear_queued_user_keeps_bus_input_for_the_next_native_call(native_backend):
    native = native_backend
    await native.author_history()
    saved = native.session.read_bytes()
    gate = asyncio.Event()
    native.provider.response_gate = gate
    async with native.open_owner(runtime_enabled=True, auto_wake=True) as (agent, session):
        comms = agent._comms
        owner = comms.registry.require(session)

        def declare_sender():
            comms.registry.declare(comms.registry.new_thread('sender', frozenset(), str(native.project)))
            with Coordination(str(native.root / "coordination.sqlite3")) as store:
                install_private_cohort_schema(store)
                store.participants.register(owner.incarnation.lookup, session, session, committed=True)

        await Coordination.run_worker(declare_sender)
        wire = Comms(native.root)
        try:
            await Coordination.run_worker(partial(
                wire.messaging.send, "sender", session, "P71_SELECTED_ORIGINAL",
            ))
            await wait_for(lambda: native.provider.posts == 1)
            wake = agent.inputs.state(session).wake_task
            native_owner = agent.turns.persistent_backends[session]
            forwarding = next(turn.inputs for turn in backend.TurnSession.active.values()
                              if turn.native_session is native_owner)
            selected_child = native_owner.child_state.child.proc
            if selected_child not in native.children:
                native.children.append(selected_child)
            response = await agent.prompt(
                session, [{"type": "text", "text": "P71_USER_TO_CLEAR"}],
                field_meta=encode_request(QueuePromptRequest("P71_USER_TO_CLEAR")),
            )
            accepted = next(update for update in decode_updates(response.field_meta)
                            if isinstance(update, InputDeliveryChangedUpdate))
            await wait_for(lambda: accepted.input_id in forwarding.accepted)
            user_key = "acp:" + accepted.input_id
            user = stored_inputs(agent.inputs.dispositions.root).lookup(user_key)
            assert user.has_native_binding and not user.has_started
            bus_message = await Coordination.run_worker(partial(
                wire.messaging.send, "sender", session, "P71_BUS_MUST_SURVIVE",
            ))
            bus_key = InputDispositions.bus_key(bus_message.message, owner)
            await wait_for(lambda: stored_inputs(agent.inputs.dispositions.root).lookup(bus_key).has_native_binding)
            bus = stored_inputs(agent.inputs.dispositions.root).lookup(bus_key)
            bus_item = next(item for item in agent.inputs.state(session).queued.values()
                            if item.key == bus_key)
            await wait_for(lambda: bus_item.input_id in forwarding.accepted)
            assert set(forwarding.pending) == {user.native_id, bus.native_id}
            await agent.inputs.clear_queued_inputs(session)
            await wait_for(lambda: stored_inputs(agent.inputs.dispositions.root).lookup(user_key).public_status == "cleared")
            cleared = stored_inputs(agent.inputs.dispositions.root).lookup(user_key)
            assert isinstance(cleared, ClearedNativeInput)
            assert cleared.native_id == user.native_id and cleared.turn_id == user.turn_id
            assert not cleared.has_started and not cleared.unresolved
            assert user.native_id not in forwarding.pending
            assert accepted.input_id not in forwarding.accepted
            assert set(forwarding.pending) == {bus.native_id}
            assert bus_item.input_id in agent.inputs.state(session).queued
            assert stored_inputs(agent.inputs.dispositions.root).lookup(bus_key) == bus
            assert native.provider.posts == 1
            gate.set()
            await asyncio.wait_for(wake, 30)
            assert native.provider.posts == 2
            assert "P71_USER_TO_CLEAR" not in json.dumps(native.provider.requests)
            assert "P71_BUS_MUST_SURVIVE" in json.dumps(native.provider.requests[-1])
            assert stored_inputs(agent.inputs.dispositions.root).lookup(user_key) == cleared
            assert stored_inputs(agent.inputs.dispositions.root).lookup(bus_key).has_started
            assert not forwarding.pending and not forwarding.accepted
            assert agent.inputs.state(session).inbox is None
            assert session not in agent.turns.turn_tasks
            assert not comms.registry.require(session).active_turn.phase.busy
            assert native.session.read_bytes().startswith(saved)
            history = Path(comms.registry.require(session).session_file)
            rows = map(json.loads, history.read_text().splitlines())
            users = [row["message"] for row in rows
                     if row.get("type") == "message" and row["message"].get("role") == "user"]
            assert sum("P71_BUS_MUST_SURVIVE" in str(user) for user in users) == 1
            assert sum("P71_SELECTED_ORIGINAL" in str(user) for user in users) == 1
            assert not any("P71_USER_TO_CLEAR" in str(user) for user in users)
        finally:
            gate.set()
    assert all(not child.alive() for child in native.children)
