"""Unstarted native input failure restores the exact original, without providers."""

from __future__ import annotations

import pytest

from agent_comms import agent_events as events
from agent_comms.coordinator import Coordination
from agent_comms.acp_extension import (
    InputFailedUpdate,
    decode_updates,
)
from test_backend_native_lifecycle import native_backend as native_backend


@pytest.mark.parametrize("terminal", [False, True])
async def test_unstarted_input_failure_restores_exact_original_without_replay(native_backend, terminal):
    native = native_backend
    await native.author_history()
    saved = native.session.read_bytes()
    updates = []

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"])

    async with native.open_owner() as (agent, session_id):
        async with native.original_input(agent, session_id, "lost prompt") as turn:
            if terminal:
                # The original native failure owner settles unbound input before
                # reporting its terminal error; error observation alone does not.
                from agent_comms.input_attempt import NotSentInput
                assert await Coordination.run_worker(turn.settle_unbound) is NotSentInput
            event = (events.Done("Pi preflight ended before attestation", False)
                     if terminal else events.Error("Pi preflight ended before attestation"))
            await agent._emit_event(session_id, event, Client())
            failed = tuple(fact for fact in decode_updates(updates[-1].field_meta)
                           if isinstance(fact, InputFailedUpdate))
            assert len(failed) == 1 and failed[0].text == "lost prompt"
            assert failed[0].failure.description == "Pi preflight ended before attestation"
            key = turn.original_keys[0]
            original = agent.inputs.dispositions.read().lookup(key)
            assert original.declared_name == ("not_sent" if terminal else "reserved")
            assert not original.has_native_binding
        # Retiring that acquired turn preserves its input for explicit recovery.
        preserved = agent.inputs.dispositions.read().lookup(key)
        assert preserved.declared_name == "not_sent"
        async with native.original_input(agent, session_id, "distinct explicit input") as next_turn:
            assert next_turn.original_keys != (key,)
            await agent._emit_event(session_id, events.Error("distinct preflight failure"), Client())
            failed = tuple(fact for fact in decode_updates(updates[-1].field_meta)
                           if isinstance(fact, InputFailedUpdate))
            assert len(failed) == 1 and failed[0].text == "distinct explicit input"
            assert agent.inputs.dispositions.read().lookup(key) == preserved
    assert native.session.read_bytes() == saved and native.provider.posts == 0
