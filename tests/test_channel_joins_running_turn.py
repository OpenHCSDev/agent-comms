"""A channel message reaches a running turn at its next tool call, like a DM."""

from agent_comms.cohort_foreground import _accept_visible_deliveries
from agent_comms.coordination_cohort import pending_sealed_assignments
from agent_comms.coordinator import Coordination
from agent_comms.field_codec import TurnId
from agent_comms.lifecycle_capability import Active
from agent_comms.queued_input import DeliveredInput
from agent_comms.turn_goal_permission import InactiveGoalPermission, TurnGoalPermission
from agent_comms.turn_input_source import SelectedOriginalInput
from agent_comms.wake_policy import MessageAudience
from conftest import publish_initial
from test_acp_private_nk_delivery import _session
from test_coordinated_runtime import tmp_path as private_root_fixture

tmp_path = private_root_fixture


async def accepted_channel_post(comms, root_id, body):
    owner = comms.registry.require("beta")
    message = publish_initial(comms, "sender", "#team", body)
    store_path = comms.root / "coordination.sqlite3"
    await _accept_visible_deliveries(
        comms.bus, root_id, store_path, owner.incarnation.lookup, owner_name=owner.name,
    )
    with Coordination(str(store_path)) as store:
        pending = pending_sealed_assignments(store, owner.incarnation.lookup)
    return owner, message, pending


async def test_agent_channel_post_joins_a_busy_turn(tmp_path):
    comms, agent, root_id = _session(tmp_path)
    try:
        comms.agents.begin_turn("beta", TurnId("5c1f0e2b9d8a4f6e8b7c6d5e4f3a2b1c"), "Existing work")
        state = agent.inputs.state("beta")
        inbox = state.open_inbox()
        state.capture_first_source(SelectedOriginalInput.answering(
            "Existing work", TurnGoalPermission(InactiveGoalPermission())))
        owner, message, pending = await accepted_channel_post(comms, root_id, "arrived mid-turn")
        assert [row.audience for row in pending] == [MessageAudience.COLLECTIVE]
        await DeliveredInput.deliver_pending(
            agent.inputs, "beta", owner, comms.bus, root_id, after_seq=0)
        delivered = inbox.get_nowait()
        assert "arrived mid-turn" in delivered.content.text
        assert f'"source_seq":{message.seq}' in delivered.content.text
    finally:
        await agent.shutdown()


async def test_agent_channel_post_waits_as_wake_work_for_an_idle_unit(tmp_path):
    comms, agent, root_id = _session(tmp_path)
    try:
        _owner, message, pending = await accepted_channel_post(comms, root_id, "wake me")
        assert [(row.wire_seq, row.audience) for row in pending] == [
            (message.seq, MessageAudience.COLLECTIVE)]
        assert isinstance(pending[0].lifecycle.mode, Active)  # The ordinary wake starts its turn.
    finally:
        await agent.shutdown()
