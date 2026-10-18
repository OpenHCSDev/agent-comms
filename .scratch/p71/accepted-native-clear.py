"""Selective queue cancellation through a real saved owner and native child."""

import asyncio
import json
from functools import partial
from pathlib import Path

from agent_comms.acp_extension import InputDeliveryChangedUpdate, QueuePromptRequest, decode_updates, encode_request
from agent_comms.backend import TurnSession
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.comms import Comms
from agent_comms.coordinator import Coordination
from agent_comms.input_attempt import ClearedNativeInput
from agent_comms.input_disposition import InputDispositions
from agent_comms.threads import Thread
from stored_inputs import stored_inputs
from test_acp_input_disposition import wait_for
from test_backend_native_lifecycle import native_backend as native_backend


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
            comms.registry.declare(Thread("sender", frozenset(), str(native.project)))
            with Coordination(str(native.root / "coordination.sqlite3")) as store:
                install_private_cohort_schema(store)
                store.participants.register(owner.incarnation.lookup, session, session, committed=True)

        await Coordination.run_worker(declare_sender)
        wire = Comms(native.root, private_initial_writes=True)
        try:
            await Coordination.run_worker(partial(
                wire.messaging.send_message, "sender", session, "P71_SELECTED_ORIGINAL",
            ))
            await wait_for(lambda: native.provider.posts == 1)
            wake = agent.inputs.wake_tasks[session]
            backend = agent.turns.persistent_backends[session]
            forwarding = next(turn.inputs for turn in TurnSession.active.values()
                              if turn.native_session is backend)
            selected_child = backend.custody.child.proc
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
                wire.messaging.send_message, "sender", session, "P71_BUS_MUST_SURVIVE",
            ))
            bus_key = InputDispositions.bus_key(bus_message, owner)
            await wait_for(lambda: stored_inputs(agent.inputs.dispositions.root).lookup(bus_key).has_native_binding)
            bus = stored_inputs(agent.inputs.dispositions.root).lookup(bus_key)
            bus_item = next(item for item in agent.inputs.queued_inputs[session].values()
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
            assert bus_item.input_id in agent.inputs.queued_inputs[session]
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
            assert session not in agent.inputs.backend_inboxes
            assert session not in agent.turns.turn_tasks
            assert not comms.registry.require(session).turn_state.busy
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
