"""Saved SDK owner input ownership over actual native pipes and the owner socket."""

import asyncio
import json
import os
import sys
from dataclasses import replace
from functools import partial
from pathlib import Path

import pytest
from acp import RequestError
from acp.schema import TextContentBlock

import agent_comms.backend as backend
import agent_comms.pi_commands as commands
import agent_comms.pi_events as pi
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.acp_failure import RequestFailure
from agent_comms.input_attempt import UnresolvedInput
from agent_comms.acp_extension import InputDeliveryChangedUpdate, QueuePromptRequest, SteerPromptRequest, decode_updates, encode_request
from agent_comms.child_process import BoundedRun, ExitedOutcome
from agent_comms.assignment_states import DeliveredAssignment
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.comms import Comms
from agent_comms.config_options import ModelConfigOption
from agent_comms.field_codec import FieldCodec
from agent_comms.following_inputs import OwnerFollowingInputs
from agent_comms.coordination_cohort import sealed_cohort_assignments
from agent_comms.coordinator import Coordination
from agent_comms.threads import Thread
from agent_comms.input_disposition import InputDispositions
from agent_comms.native_pi import NativePiUnavailable
from agent_comms.notification_progress import AnsweredProgress, SentProgress, ShownToModelProgress
from agent_comms.pi_native_backend import PersistentPiSession
from agent_comms.pi_rpc import PiRpcChannel
from agent_comms.runtime import RuntimeProxy, socket_path
from delivery_owner_fixture import canonical_agent
from stored_inputs import sent_text, stored_inputs
from native_backend_fixture import wait_for


class Updates:
    def __init__(self):
        self.rows = []

    async def session_update(self, *, session_id, update):
        self.rows.append(update if isinstance(update, dict) else
                         update.model_dump(by_alias=True, exclude_none=True))


async def test_native_preflight_failure_is_visible_and_cannot_mark_started(native_backend, monkeypatch):
    native = native_backend
    await native.author_history()
    saved = native.session.read_bytes()
    async with native.open_owner() as (owner, session):
        updates = Updates()
        owner.on_connect(updates)

        async def refused_preflight(self, launch, *, reuse, startup, watchdog):
            # Refuse this prompt's native resource borrow after authentic
            # SDK source/identity preparation.
            raise NativePiUnavailable("native preflight refused before prompt")

        monkeypatch.setattr(PersistentPiSession, "open", refused_preflight)
        with pytest.raises(RequestError) as caught:
            await owner.prompt(session, [TextContentBlock(type="text", text="test")])
        assert isinstance(caught.value.__cause__, NativePiUnavailable)
        assert "preflight" in str(caught.value.__cause__)
        failure = RequestFailure.from_error(caught.value)
        assert failure.notification_published and failure.failure.input_state.public_status == "not_sent"
        rows = stored_inputs(owner.inputs.dispositions.root).unknown(frozenset({session}))
        assert len(rows) == 1 and not rows[0].has_native_binding
        assert rows[0].public_status == "not_sent"
        assert not any(state.pending_turns for state in owner.inputs.drained.values()) and not owner.turns.turn_state(session).active.phase.busy
        assert any(isinstance(fact, InputDeliveryChangedUpdate)
                   for update in updates.rows for fact in decode_updates(update.get("_meta")))
        assert Comms(native.root).goals.unresolved_inputs(session) == tuple(UnresolvedInput.of(row) for row in rows)
    assert native.session.read_bytes() == saved and native.provider.posts == 0


@pytest.mark.parametrize("second_start", [False, True])
async def test_late_owner_socket_observes_unknown_until_exact_native_start(
    native_backend, monkeypatch, second_start
):
    native = native_backend
    await native.author_history()
    saved = native.session.read_bytes()
    native.provider.response_gate = asyncio.Event()
    acknowledgements = []
    prompts = {}
    decode = PiRpcChannel.decode_record
    encode = PiRpcChannel.command_bytes

    def observed_command(command):
        frame = encode(command)
        if isinstance(command, commands.Prompt):
            prompts[command.input_id] = command
        return frame

    def observed_response(raw, **options):
        event = decode(raw, **options)
        if isinstance(event, pi.Response) and event.command is commands.Prompt:
            acknowledgements.append(event)
        return event

    monkeypatch.setattr(PiRpcChannel, "decode_record", staticmethod(observed_response))
    monkeypatch.setattr(PiRpcChannel, "command_bytes", staticmethod(observed_command))
    async with native.open_owner(runtime_enabled=True) as (owner, session):
        turn = asyncio.create_task(owner.prompt(session, [TextContentBlock(type="text", text="first")]))
        attachment = canonical_agent(owner._comms, auto_wake=False)
        updates = Updates()
        attachment.on_connect(updates)
        proxy = RuntimeProxy(attachment, session, socket_path(native.root, os.getpid()))
        try:
            await wait_for(lambda: native.provider.posts == 1)
            # Subscribe after the original input starts, before transferring
            # this socket controller's distinct follow-up into owned ownership.
            await proxy.subscribe()
            accepted = await proxy.request(
                "prompt", prompt=[{"type": "text", "text": "followup"}],
                meta=encode_request(QueuePromptRequest()),
            )
            (acknowledgement,) = decode_updates(accepted["_meta"])
            assert isinstance(acknowledgement, InputDeliveryChangedUpdate)
            public_id = acknowledgement.input_id
            key = "acp:" + public_id
            await wait_for(lambda: any(event.id == public_id and event.success
                                      for event in acknowledgements))
            pending = await proxy.request("input_dispositions")
            assert any(isinstance(fact, InputDeliveryChangedUpdate)
                       for update in updates.rows for fact in decode_updates(update.get("_meta")))
            assert [row["inputId"] for row in pending["inputs"]] == [public_id]
            unknown = stored_inputs(owner.inputs.dispositions.root).lookup(key)
            assert unknown.unresolved and unknown.has_native_binding
            assert not unknown.has_started  # Original RPC ACK is not native STARTED.
            assert len(native.saved_inputs()) == 2 and native.provider.posts == 1
            if second_start:
                native.provider.response_gate.set()
            else:
                # Retire this exact private native child before the queued
                # input starts. Its original sent intent remains UNKNOWN.
                await backend.terminate_task_process(turn)
            if second_start:
                assert (await asyncio.wait_for(turn, 30)).stop_reason == "end_turn"
            else:
                with pytest.raises(RequestError) as caught:
                    await asyncio.wait_for(turn, 30)
                failure = RequestFailure.from_error(caught.value)
                assert failure.notification_published
            final = stored_inputs(owner.inputs.dispositions.root).rows
            assert len(final) == 2
            assert final[key].unresolved is (not second_start)
            assert final[key].has_started is second_start
            assert len({row.native_id for row in final.values()}) == 2
            sent = prompts[final[key].native_id]
            assert sent.id == public_id
            assert final[key].sent_digest.matches(sent.message)
            assert sent.message.endswith("followup")
            if second_start:
                assert sent_text(native, final[key]) == sent.message
            assert len(native.saved_inputs()) == (3 if second_start else 2)
            overview = await proxy.request("input_dispositions", include_history=True)
            assert overview["inputs"] == []
            assert overview["historicalCount"] == (0 if second_start else 1)
            cleared = await proxy.request("dismiss_historical_inputs")
            assert cleared["dismissedHistoricalCount"] == (0 if second_start else 1)
            reopened = stored_inputs(owner.inputs.dispositions.root).rows
            assert {k: replace(v, notice_dismissed=False) for k, v in reopened.items()} == final
            before = (native.session.read_bytes(), native.provider.posts)
            assert await owner.inputs.drain_inbox(session) == 0
            assert not any(state.pending_turns for state in owner.inputs.drained.values()) and not any(state.wake_task for state in owner.inputs.drained.values())
            assert stored_inputs(owner.inputs.dispositions.root).rows == reopened
            assert (native.session.read_bytes(), native.provider.posts) == before
            assert native.session.read_bytes().startswith(saved)
            assert not owner.turns.turn_state(session).active.phase.busy and not any(state.inbox for state in owner.inputs.drained.values())
        finally:
            native.provider.response_gate.set()
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)
            await proxy.close()
            await attachment.shutdown()
    assert all(not child.alive() and not child.platform.group_members(child.identity)
               for child in native.children)


async def test_native_steer_keeps_original_input_and_turn_cleanup(native_backend):
    native = native_backend
    await native.author_history()
    saved = native.session.read_bytes()
    original_response = asyncio.Event()
    native.provider.response_gate = original_response
    async with native.open_owner(runtime_enabled=True) as (owner, session):
        turn = asyncio.create_task(owner.prompt(session, [TextContentBlock(type="text", text="first")]))
        attachment = canonical_agent(owner._comms, auto_wake=False)
        attachment.on_connect(Updates())
        proxy = RuntimeProxy(attachment, session, socket_path(native.root, os.getpid()))
        try:
            await proxy.subscribe()
            await wait_for(lambda: native.provider.posts == 1)
            lease = owner._comms.registry.require(session).require_turn_lease()
            # The first actual response remains held; native interrupt must
            # consume the distinct steer before it can finish this turn.
            native.provider.response_gate = None
            accepted = await proxy.request(
                "prompt", prompt=[{"type": "text", "text": "steer now"}],
                meta=encode_request(SteerPromptRequest()),
            )
            (acknowledgement,) = decode_updates(accepted["_meta"])
            assert isinstance(acknowledgement, InputDeliveryChangedUpdate)
            assert (await asyncio.wait_for(turn, 30)).stop_reason == "end_turn"
            rows = stored_inputs(owner.inputs.dispositions.root).rows
            assert len(rows) == 2 and all(row.has_started for row in rows.values())
            assert sent_text(native, rows["acp:" + acknowledgement.input_id]).endswith("steer now")
            assert {row.turn_id for row in rows.values()} == {lease.turn_id}
            assert len({row.native_id for row in rows.values()}) == 2
            assert native.provider.posts == 2 and len(native.saved_inputs()) == 3
            assert native.session.read_bytes().startswith(saved)
            assert not owner.turns.turn_state(session).active.phase.busy and not any(state.inbox for state in owner.inputs.drained.values())
        finally:
            original_response.set()
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)
            await proxy.close()
            await attachment.shutdown()
    assert all(not child.alive() and not child.platform.group_members(child.identity)
               for child in native.children)


async def test_bus_message_arriving_mid_turn_is_taken_by_the_running_turn(
    native_backend, monkeypatch,
):
    native = native_backend
    await native.author_history()
    held = asyncio.Event()
    native.provider.response_gate = held
    async with native.open_owner(runtime_enabled=True, auto_wake=True) as (owner, session):
        comms = owner._comms
        root_id = owner.configured_root_id
        receiver = await Coordination.run_worker(partial(comms.registry.require, session))
        lookup = RecordedThreadIncarnation("fixture", receiver.created_at).lookup

        def prepare():
            comms.registry.register(
                comms.registry.new_thread(name='sender', tags=frozenset(), worktree=str(native.project), created_at=61001.0)
            )
            with Coordination(str(native.root / "coordination.sqlite3")) as store:
                install_private_cohort_schema(store)
                store.participants.register(lookup, session, session, committed=True)

        await Coordination.run_worker(prepare)
        turn = asyncio.create_task(
            owner.prompt(session, [TextContentBlock(type="text", text="first")])
        )
        try:
            await wait_for(lambda: native.provider.posts == 1)
            message = await Coordination.run_worker(partial(
                Comms(native.root).messaging.send_message,
                "sender", session, "are you still on the parser?",
            ))
            native.provider.response_gate = None
            # The owner's own observation hands the message to the turn.
            await wait_for(lambda: len(stored_inputs(owner.inputs.dispositions.root).rows) == 2)
            held.set()
            assert (await asyncio.wait_for(turn, 30)).stop_reason == "end_turn"

            def assignments():
                with Coordination(str(native.root / "coordination.sqlite3")) as store:
                    return sealed_cohort_assignments(store, lookup)

            (assignment,) = await Coordination.run_worker(assignments)
            assert assignment.wire_seq == message.seq
            assert type(assignment.lifecycle) is DeliveredAssignment
            def forbid_native_evidence(*_args, **_kwargs):
                raise AssertionError("notification projection decoded native evidence")

            monkeypatch.setattr(
                "agent_comms.native_entries.NativeEntry.read_evidence",
                forbid_native_evidence,
            )
            sender_view = comms.views.thread_presentations(("sender",)).require("sender")
            assert sender_view is not None
            sent_read = next(
                outcome for outcome in sender_view.sent_notifications
                if outcome.message.reference == message.reference
            )
            assert sent_read.state.startswith("Read ")
            assert sent_read.read_at_ms == assignment.updated_at_ms
            assert sent_read.changed
            # One turn, two inputs: the prompt and the message, each sent once.
            assert native.provider.posts == 2
            taken = native.saved_inputs()[-1]
            assert "are you still on the parser?" in json.dumps(taken)
            assert "a message arrived while you work" in json.dumps(taken)
            rows = stored_inputs(owner.inputs.dispositions.root).rows
            assert len(rows) == 2 and all(row.has_started for row in rows.values())
            assert len({row.turn_id for row in rows.values()}) == 1
            # Nothing is left to wake for once the turn ends.
            assert await owner._drain_private_nk(session, root_id) == 0
            assert native.provider.posts == 2
        finally:
            held.set()
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)


async def test_waiting_message_is_drained_when_following_input_opens(
    native_backend, monkeypatch,
):
    native = native_backend
    await native.author_history()
    native.provider.tool_call = (
        "bash", {"command": "sleep 2; printf slow-tool-done"},
    )
    opened, open_release = asyncio.Event(), asyncio.Event()
    started, started_release = asyncio.Event(), asyncio.Event()
    original_open = OwnerFollowingInputs.open
    original_started = OwnerFollowingInputs.started

    async def delayed_open(following, tracked):
        opened.set()
        await open_release.wait()
        await original_open(following, tracked)

    async def delayed_started(following, input_id):
        started.set()
        await started_release.wait()
        return await original_started(following, input_id)

    monkeypatch.setattr(OwnerFollowingInputs, "open", delayed_open)
    monkeypatch.setattr(OwnerFollowingInputs, "started", delayed_started)
    async with native.open_owner(
        runtime_enabled=True, auto_wake=True, native_options=("--tools", "bash"),
    ) as (owner, session):
        comms = owner._comms
        receiver = await Coordination.run_worker(partial(comms.registry.require, session))
        lookup = RecordedThreadIncarnation("fixture", receiver.created_at).lookup

        def prepare():
            comms.registry.register(
                comms.registry.new_thread(name='sender', tags=frozenset(), worktree=str(native.project), created_at=61002.0)
            )
            with Coordination(str(native.root / "coordination.sqlite3")) as store:
                install_private_cohort_schema(store)
                store.participants.register(lookup, session, session, committed=True)

        await Coordination.run_worker(prepare)
        initial = await Coordination.run_worker(partial(
            Comms(native.root).messaging.send_message,
            "sender", session, "first",
        ))
        try:
            await asyncio.wait_for(opened.wait(), 30)
            # Pi has recorded the tool call and is inside its deliberately slow
            # implementation while Core still has not opened following input.
            await wait_for(lambda: b'"id":"local-tool-once"' in native.session.read_bytes())
            message = await Coordination.run_worker(partial(
                Comms(native.root).messaging.send_message,
                "sender", session, "deliver as soon as following input opens",
            ))
            await wait_for(lambda: owner.inputs.state(session).idle_revision is not None)
            key = message.seq, message.message_id
            pending, = comms.views.message_notifications_for_references((message.reference,))[key]
            assert pending.label == "Sent" and not pending.state.working
            assert not pending.state.read and not pending.state.delivered
            open_release.set()
            await asyncio.wait_for(started.wait(), 30)
            in_flight, = comms.views.message_notifications_for_references((message.reference,))[key]
            assert isinstance(in_flight.state, SentProgress)
            assert not in_flight.state.read and not in_flight.state.delivered
            started_release.set()
            # Opening is the retry trigger; no second bus append or observation
            # tick is needed to reserve the waiting input.
            await wait_for(lambda: any(
                "deliver as soon as following input opens" in sent_text(native, row)
                for row in stored_inputs(owner.inputs.dispositions.root).rows.values()
                if row.has_native_binding
            ))
            await wait_for(lambda: not owner.turns.turn_state(session).active.phase.busy)

            with Coordination(str(native.root / "coordination.sqlite3")) as store:
                assignments = sealed_cohort_assignments(store, lookup)
            assert [assignment.wire_seq for assignment in assignments] == [
                initial.seq, message.seq,
            ]
            assert type(assignments[-1].lifecycle) is DeliveredAssignment
            read, = comms.views.message_notifications_for_references((message.reference,))[key]
            assert isinstance(read.state, AnsweredProgress) and not read.state.working
            assert isinstance(read.state.proof, ShownToModelProgress)
            assert read.state.proof.read_at_ms == assignments[-1].updated_at_ms
            assert read.answer is not None
            assert native.provider.posts == 2
            rows = [json.loads(line) for line in native.session.read_text().splitlines()]
            tool_result = next(
                index for index, row in enumerate(rows)
                if row.get("type") == "message"
                and row["message"].get("role") == "toolResult"
                and row["message"].get("toolCallId") == "local-tool-once"
            )
            following = next(
                index for index, row in enumerate(rows)
                if "deliver as soon as following input opens" in json.dumps(row)
            )
            assert tool_result < following
        finally:
            open_release.set()
            started_release.set()


async def test_waiting_messages_follow_the_original_on_the_changed_model(
    native_backend,
):
    native = native_backend
    await native.author_history()
    held = asyncio.Event()
    native.provider.response_gate = held
    async with native.open_owner(runtime_enabled=True, auto_wake=True) as (owner, session):
        comms = owner._comms
        receiver = await Coordination.run_worker(partial(comms.registry.require, session))
        lookup = RecordedThreadIncarnation("fixture", receiver.created_at).lookup

        def prepare():
            comms.registry.register(
                comms.registry.new_thread(name='sender', tags=frozenset(), worktree=str(native.project), created_at=61001.0)
            )
            with Coordination(str(native.root / "coordination.sqlite3")) as store:
                install_private_cohort_schema(store)
                store.participants.register(lookup, session, session, committed=True)

        await Coordination.run_worker(prepare)
        turn = asyncio.create_task(
            owner.prompt(session, [TextContentBlock(type="text", text="first")])
        )
        try:
            await wait_for(lambda: native.provider.posts == 1)
            assert native.provider.requests[0]["model"] == "fixture"
            sender = Comms(native.root).messaging
            for body in ("first question", "second question"):
                await Coordination.run_worker(partial(
                    sender.send_message, "sender", session, body
                ))
            native.provider.response_gate = None
            await owner.set_config_option(
                FieldCodec.encode(ModelConfigOption), session, "response-local/fixture-second"
            )
            await wait_for(lambda: len(stored_inputs(owner.inputs.dispositions.root).rows) == 3)
            held.set()
            assert (await asyncio.wait_for(turn, 30)).stop_reason == "end_turn"
            # Busy bus inputs steer the next model call, rather than creating
            # a separate follow-up response for each message.
            assert native.provider.posts == 2
            original, following = native.provider.requests
            assert following["model"] == "fixture-second"
            previous = [message for message in following["messages"]
                        if message["role"] == "assistant"][-1]
            assert previous["content"] == native.provider.text
            for body in ("first question", "second question"):
                assert body not in json.dumps(original["messages"])
                assert body in json.dumps(following["messages"])
            rows = stored_inputs(owner.inputs.dispositions.root).rows
            assert len(rows) == 3 and all(row.has_started for row in rows.values())
        finally:
            held.set()
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)


async def test_hard_exit_after_owner_acceptance_preserves_unknown_without_replay(native_backend):
    native = native_backend
    await native.author_history()
    saved = native.session.read_bytes()
    code = """import asyncio, os, sys
from pathlib import Path
sys.path[:0] = [sys.argv[4], sys.argv[5]]
from delivery_owner_fixture import canonical_agent
from native_backend_fixture import NativeBackendFixture
from agent_comms.comms import Comms
from agent_comms.queued_input import InitialInput
from stored_inputs import sent_text, stored_inputs
async def main():
    owner = canonical_agent(Comms(Path(sys.argv[1])), auto_wake=False,
                            agent_args=['--provider','response-local','--model','fixture','--thinking','off'])
    session = await NativeBackendFixture.attach_saved_owner(
        owner, project=Path(sys.argv[2]), session=Path(sys.argv[3]))
    async def die(*args, **kwargs):
        os._exit(17)
    owner.inputs.emit_input_disposition = die
    await InitialInput.run(owner.inputs,session,session,'Retain exact crash input')
asyncio.run(main())
"""
    result = await BoundedRun.run(
        (sys.executable, "-c", code, str(native.root), str(native.project),
         str(native.session), str(Path(__file__).parent), str(Path(__file__).parents[1] / "src")),
        timeout=15, cwd=native.project,
    )
    assert result.outcome == ExitedOutcome(17), result.stderr.decode()
    comms = Comms(native.root)
    session = native.project.name
    ledger = InputDispositions(native.root)
    before = ledger.path.read_bytes()
    rows = stored_inputs(ledger.root).unknown(frozenset({session}))
    assert len(rows) == 1 and rows[0].source_text == "Retain exact crash input"
    assert not rows[0].has_native_binding and not rows[0].has_started
    original = comms.registry.require(session)
    assert not original.process_alive
    comms.threads.restore_stopped(comms.registry.snapshot(), (session,))
    thread = comms.owners.acquire_thread(session, owner_pid=os.getpid())
    owner = canonical_agent(comms, auto_wake=False)
    try:
        await owner.sessions.bind_owned(thread, session)
        assert await owner.inputs.drain_inbox(session) == 0
        assert not any(state.pending_turns for state in owner.inputs.drained.values()) and not any(state.wake_task for state in owner.inputs.drained.values())
        assert ledger.path.read_bytes() == before
        assert native.session.read_bytes() == saved and native.provider.posts == 0
    finally:
        await owner.shutdown()


@pytest.mark.refactor_guard
def test_disposition_fixtures_cannot_reintroduce_retired_delivery_engine():
    import ast

    for name in (
        "test_acp_input_disposition.py",
        "test_acp_channel_disposition.py",
        "delivery_owner_fixture.py",
    ):
        tree = ast.parse(Path(__file__).with_name(name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"delivery_cursors", "inbox_cursors", "legacy_through"}
            if isinstance(node, ast.ImportFrom):
                assert not {alias.name for alias in node.names} & {
                    "AcpDeliveryCursors",
                    "DeliveryCursor",
                    "DeliveryDocument",
                    "wire",
                }


async def test_bus_message_arriving_during_a_reply_turn_is_taken_by_that_turn(native_backend):
    native = native_backend
    await native.author_history()
    held = asyncio.Event()
    native.provider.response_gate = held
    async with native.open_owner(runtime_enabled=True, auto_wake=True) as (owner, session):
        comms = owner._comms
        receiver = await Coordination.run_worker(partial(comms.registry.require, session))
        lookup = RecordedThreadIncarnation("fixture", receiver.created_at).lookup
        comms.threads.claim_thread("sender", tags=frozenset(), worktree=str(native.project))
        try:
            first = comms.messaging.send_message("sender", session, "which parser is slow?")
            await owner.inputs.drain_inbox(session)
            await wait_for(lambda: native.provider.posts == 1)
            reply_turn = owner.inputs.state(session).wake_task
            known = len(stored_inputs(owner.inputs.dispositions.root).rows)
            assert native.provider.requests[0]["model"] == "fixture"
            elsewhere = Comms(native.root)

            def store_change():
                thread = elsewhere.registry.require(session)
                elsewhere.threads.set_thread_configuration(
                    thread, model="response-local/fixture-second",
                    thinking_level=thread.thinking_level,
                )

            await Coordination.run_worker(store_change)
            second = comms.messaging.send_message("sender", session, "the yaml one, I mean")
            native.provider.response_gate = None
            # One observer feeds settings and accepted messages through the running-turn inbox.
            await wait_for(
                lambda: len(stored_inputs(owner.inputs.dispositions.root).rows) == known + 1
                and owner.sessions.config.settings_in_use[session].model
                == "response-local/fixture-second"
            )
            held.set()
            await asyncio.wait_for(reply_turn, 30)

            def assignments():
                with Coordination(str(native.root / "coordination.sqlite3")) as store:
                    return sealed_cohort_assignments(store, lookup)

            states = {
                assignment.wire_seq: type(assignment.lifecycle)
                for assignment in await Coordination.run_worker(assignments)
            }
            assert states[second.seq] is DeliveredAssignment
            assert states[first.seq] is not DeliveredAssignment
            # One turn took both: the second went in at the next model call, sent once.
            assert native.provider.posts == 2
            assert native.provider.requests[1]["model"] == "fixture-second"
            assert "the yaml one, I mean" in json.dumps(native.provider.requests[1]["messages"])
            assert "the yaml one, I mean" not in json.dumps(native.provider.requests[0]["messages"])
            assert len({row.turn_id for row in stored_inputs(owner.inputs.dispositions.root).rows.values()}) == 1
            # Nothing is left to start another turn for, and the sender got one answer.
            await wait_for(lambda: owner.inputs.state(session).wake_task is None)
            assert native.provider.posts == 2
            assert len([m for m in list(comms.views.full_history_page().messages) if m.sender == session]) == 1
        finally:
            held.set()


async def test_model_change_stored_by_another_process_reaches_the_running_turn(native_backend):
    native = native_backend
    await native.author_history()
    held = asyncio.Event()
    native.provider.response_gate = held
    async with native.open_owner(runtime_enabled=True, auto_wake=True) as (owner, session):
        comms = owner._comms
        comms.threads.claim_thread("sender", tags=frozenset(), worktree=str(native.project))
        turn = asyncio.create_task(
            owner.prompt(session, [TextContentBlock(type="text", text="first")])
        )
        try:
            await wait_for(lambda: native.provider.posts == 1)
            assert native.provider.requests[0]["model"] == "fixture"
            # What `agent-comms invoke --tool comms_model` does from its own process.
            elsewhere = Comms(native.root)

            def store_change():
                thread = elsewhere.registry.require(session)
                elsewhere.threads.set_thread_configuration(
                    thread, model="response-local/fixture-second",
                    thinking_level=thread.thinking_level,
                )

            await Coordination.run_worker(store_change)
            # The owner sees the stored change itself and hands it to the running turn.
            await wait_for(
                lambda: owner.sessions.config.settings_in_use[session].model
                == "response-local/fixture-second"
                and owner.inputs.state(session).inbox.empty()
            )
            await Coordination.run_worker(partial(
                elsewhere.messaging.send_message, "sender", session, "still there?"
            ))
            native.provider.response_gate = None
            await wait_for(lambda: len(stored_inputs(owner.inputs.dispositions.root).rows) == 2)
            held.set()
            assert (await asyncio.wait_for(turn, 30)).stop_reason == "end_turn"
            assert native.provider.posts == 2
            assert native.provider.requests[1]["model"] == "fixture-second"
        finally:
            held.set()
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)
