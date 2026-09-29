"""Captured admissions survive only their exact live owner; dispatch failures stay uncertain."""

import pytest

from agent_comms.acp_extension import InputDeliveryChangedUpdate, decode_updates
from agent_comms.errors import RelationViolationError
from agent_comms.queued_input import InputHandoffRefused
from test_acp_queue_contract import _owner


async def test_initial_capture_rejects_owner_readmission_during_acceptance_notice(
    tmp_path, monkeypatch
):
    comms, agent, _, _ = _owner(tmp_path)
    admitted = []
    dispatched = []

    class Client:
        async def session_update(self, **kwargs):
            notices = decode_updates(kwargs["update"].field_meta)
            for notice in notices:
                if isinstance(notice, InputDeliveryChangedUpdate):
                    admitted.append(notice.input_id)
                    owner = comms.registry.require("beta")
                    comms.registry.register(owner, new_owner=True)

    async def forbidden(*args, **kwargs):
        dispatched.append(args)
        raise AssertionError("Changed captured owner must not reach the native dispatcher")

    agent.on_connect(Client())
    monkeypatch.setattr(agent.turns, "run_agent_turn", forbidden)
    try:
        with pytest.raises(InputHandoffRefused, match="context changed"):
            await agent.inputs.run_owned_input("beta", "beta", "Fresh captured input")
        assert len(admitted) == 1 and not dispatched
        row = agent.inputs.dispositions.read().lookup("acp:" + admitted[0])
        assert row.accepts_reservation and not row.has_native_binding
    finally:
        await agent.shutdown()


async def test_postdispatch_failure_is_not_reclassified_as_admission_refusal(tmp_path, monkeypatch):
    comms, agent, _, _ = _owner(tmp_path)
    entered = []
    failure = RelationViolationError("late uncertainty after dispatcher entry")

    async def uncertain(*args, **kwargs):
        entered.append(kwargs["original_keys"])
        raise failure

    monkeypatch.setattr(agent.turns, "run_agent_turn", uncertain)
    try:
        with pytest.raises(RelationViolationError) as caught:
            await agent.inputs.run_owned_input("beta", "beta", "One explicit request")
        assert caught.value is failure and not isinstance(caught.value, InputHandoffRefused)
        assert len(entered) == 1
        assert len(agent.inputs.dispositions.read().rows) == 1
    finally:
        await agent.shutdown()
