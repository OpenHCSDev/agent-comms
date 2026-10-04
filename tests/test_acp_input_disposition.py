"""Saved SDK owner input custody over actual native pipes and the owner socket."""

import asyncio
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from acp import RequestError
from acp.schema import TextContentBlock

from agent_comms import backend
from agent_comms import pi_commands as commands
from agent_comms import pi_events as pi
from agent_comms.acp_failure import PromptFailureReceipt
from agent_comms.acp_extension import InputDeliveryChangedUpdate, QueuePromptRequest, SteerPromptRequest, decode_updates, encode_request
from agent_comms.child_process import BoundedRun, ExitedOutcome
from agent_comms.comms import Comms
from agent_comms.input_disposition import InputDispositions
from agent_comms.native_pi import NativePiUnavailable
from agent_comms.native_session_prepare import NativeSessionPreparation
from agent_comms.pi_rpc import PiRpcChannel
from agent_comms.runtime import RuntimeProxy, socket_path
from delivery_owner_fixture import canonical_agent
from test_backend_native_lifecycle import native_backend


class Updates:
    def __init__(self):
        self.rows = []

    async def session_update(self, *, session_id, update):
        self.rows.append(update if isinstance(update, dict) else
                         update.model_dump(by_alias=True, exclude_none=True))


async def wait_for(predicate):
    async with asyncio.timeout(30):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_native_preflight_failure_is_visible_and_cannot_mark_started(native_backend, monkeypatch):
    native = native_backend
    await native.author_history()
    saved = native.session.read_bytes()
    async with native.open_owner() as (owner, session):
        updates = Updates()
        owner.on_connect(updates)

        async def refused_preflight(self):
            # Inject refusal at the original prepared-session boundary after
            # authentic SDK source/identity acquisition. No child, witness or
            # capability response is fabricated by the caller.
            raise NativePiUnavailable("native preflight refused before prompt")

        monkeypatch.setattr(NativeSessionPreparation, "input_ready", refused_preflight)
        with pytest.raises(RequestError) as caught:
            await owner.prompt(session, [TextContentBlock(type="text", text="test")])
        assert isinstance(caught.value.__cause__, NativePiUnavailable)
        assert "preflight" in str(caught.value.__cause__)
        failure = PromptFailureReceipt.from_error(caught.value.code, str(caught.value), caught.value.data)
        assert failure.notification_published and failure.failure.input_state.public_status == "not_sent"
        rows = owner.inputs.dispositions.read().unknown(frozenset({session}))
        assert len(rows) == 1 and not rows[0].has_native_binding
        assert rows[0].public_status == "not_sent"
        assert not owner.inputs.pending_turns and not owner.turns.turn_state(session).busy
        assert any(isinstance(fact, InputDeliveryChangedUpdate)
                   for update in updates.rows for fact in decode_updates(update.get("_meta")))
        assert Comms(native.root).goals.unresolved_inputs(session) == [row.public() for row in rows]
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
    decode = PiRpcChannel.decode_record

    def observed_response(raw, **options):
        event = decode(raw, **options)
        if isinstance(event, pi.Response) and event.command is commands.Prompt:
            acknowledgements.append(event)
        return event

    monkeypatch.setattr(PiRpcChannel, "decode_record", staticmethod(observed_response))
    async with native.open_owner(runtime_enabled=True) as (owner, session):
        turn = asyncio.create_task(owner.prompt(session, [TextContentBlock(type="text", text="first")]))
        attachment = canonical_agent(owner._comms, auto_wake=False)
        updates = Updates()
        attachment.on_connect(updates)
        proxy = RuntimeProxy(attachment, session, socket_path(native.root, os.getpid()))
        try:
            await wait_for(lambda: native.provider.posts == 1)
            # Subscribe after the original input starts, before transferring
            # this socket controller's distinct follow-up into owned custody.
            await proxy.subscribe()
            accepted = await proxy.request(
                "prompt", prompt=[{"type": "text", "text": "followup"}],
                meta=encode_request(QueuePromptRequest()),
            )
            (receipt,) = decode_updates(accepted["_meta"])
            assert isinstance(receipt, InputDeliveryChangedUpdate)
            public_id = receipt.input_id
            key = "acp:" + public_id
            await wait_for(lambda: any(event.id == public_id and event.success
                                      for event in acknowledgements))
            pending = await proxy.request("input_dispositions")
            assert any(isinstance(fact, InputDeliveryChangedUpdate)
                       for update in updates.rows for fact in decode_updates(update.get("_meta")))
            assert [row["inputId"] for row in pending["inputs"]] == [public_id]
            unknown = owner.inputs.dispositions.read().lookup(key)
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
                failure = PromptFailureReceipt.from_error(caught.value.code, str(caught.value), caught.value.data)
                assert failure.notification_published
            final = InputDispositions(owner.inputs.dispositions.path).read().rows
            assert len(final) == 2
            assert final[key].unresolved is (not second_start)
            assert final[key].has_started is second_start
            assert len({row.native_id for row in final.values()}) == 2
            assert final[key].sent_text.endswith("followup")
            assert len(native.saved_inputs()) == (3 if second_start else 2)
            overview = await proxy.request("input_dispositions", include_history=True)
            assert overview["inputs"] == []
            assert overview["historicalCount"] == (0 if second_start else 1)
            cleared = await proxy.request("dismiss_historical_inputs")
            assert cleared["dismissedHistoricalCount"] == (0 if second_start else 1)
            reopened = InputDispositions(owner.inputs.dispositions.path).read().rows
            assert {k: replace(v, notice_dismissed=False) for k, v in reopened.items()} == final
            before = (native.session.read_bytes(), native.provider.posts)
            assert await owner.inputs.drain_inbox(session) == 0
            assert not owner.inputs.pending_turns and not owner.inputs.wake_tasks
            assert owner.inputs.dispositions.read().rows == reopened
            assert (native.session.read_bytes(), native.provider.posts) == before
            assert native.session.read_bytes().startswith(saved)
            assert not owner.turns.turn_state(session).busy and not owner.inputs.backend_inboxes
        finally:
            native.provider.response_gate.set()
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)
            await proxy.close()
            await attachment.shutdown()
    assert all(not child.alive() and not child.platform.group_members(child.identity)
               for child in native.children)


async def test_native_steer_keeps_original_input_and_turn_custody(native_backend):
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
            (receipt,) = decode_updates(accepted["_meta"])
            assert isinstance(receipt, InputDeliveryChangedUpdate)
            assert (await asyncio.wait_for(turn, 30)).stop_reason == "end_turn"
            rows = owner.inputs.dispositions.read().rows
            assert len(rows) == 2 and all(row.has_started for row in rows.values())
            assert rows["acp:" + receipt.input_id].sent_text.endswith("steer now")
            assert {row.turn_id for row in rows.values()} == {lease.turn_id}
            assert len({row.native_id for row in rows.values()}) == 2
            assert native.provider.posts == 2 and len(native.saved_inputs()) == 3
            assert native.session.read_bytes().startswith(saved)
            assert not owner.turns.turn_state(session).busy and not owner.inputs.backend_inboxes
        finally:
            original_response.set()
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)
            await proxy.close()
            await attachment.shutdown()
    assert all(not child.alive() and not child.platform.group_members(child.identity)
               for child in native.children)


async def test_hard_exit_after_owner_acceptance_preserves_unknown_without_replay(native_backend):
    native = native_backend
    await native.author_history()
    saved = native.session.read_bytes()
    code = """import asyncio, os, sys
from pathlib import Path
sys.path.insert(0,sys.argv[4])
from delivery_owner_fixture import canonical_agent
from native_backend_fixture import NativeBackendFixture
from agent_comms.comms import Comms
from agent_comms.queued_input import InitialInput
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
         str(native.session), str(Path(__file__).parent)),
        timeout=15, cwd=native.project,
    )
    assert result.outcome == ExitedOutcome(17), result.stderr.decode()
    comms = Comms(native.root)
    session = native.project.name
    ledger = InputDispositions(native.root / InputDispositions.filename)
    before = ledger.path.read_bytes()
    rows = ledger.read().unknown(frozenset({session}))
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
        assert not owner.inputs.pending_turns and not owner.inputs.wake_tasks
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
