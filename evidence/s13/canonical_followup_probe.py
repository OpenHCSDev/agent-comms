"""Current ACP/bus followup reproducer; model response is suspended, no provider.

Run against the parent integration with PYTHONPATH=src:tests and pytest this file.
This isolates admission failure before any fresh native input can be sent.
"""

import asyncio

from agent_comms import cohort_foreground, coordinated_runtime
from agent_comms.goal_actions import SetGoalAction
from test_acp_private_nk_delivery import _session
from test_acp_private_nk_delivery import tmp_path as private_root_fixture
from test_coordinated_runtime import _fake_model

tmp_path = private_root_fixture


async def test_selected_direct_turn_accepts_fresh_owner_followup(tmp_path, monkeypatch):
    comms, agent, _ = _session(tmp_path)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    fake, _calls = _fake_model()
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(package, **kwargs):
        entered.set()
        await release.wait()
        return await fake(package, **kwargs)

    monkeypatch.setattr(coordinated_runtime, "run_native_pi_turn", blocked)
    original = comms.goals.update_goal("beta", SetGoalAction(text="Separate parked goal"))
    comms.messaging.send_message("sender", "beta", "Separate DM")
    turn = asyncio.create_task(agent.inputs.drain_inbox("beta"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert comms.registry.require("beta").active_turn is not None
        result = await asyncio.wait_for(
            agent.prompt("beta", [{"type": "text", "text": "Fresh owner followup"}]), 2
        )
        assert result.field_meta["agentComms"]["inputDisposition"]["status"] == "accepted_not_started"
        assert comms.registry.require("beta").goal == original
        assert not (comms.root / "goal-private").exists()
    finally:
        release.set()
        await asyncio.wait_for(turn, 5)
        await agent.shutdown()
