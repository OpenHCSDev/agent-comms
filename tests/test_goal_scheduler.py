"""Ledger readiness fences scheduling without rewriting the declared goal."""

from agent_comms.goal_actions import SetGoalAction
from agent_comms.goal_generation import ReadyGeneration, ReservedGeneration
from agent_comms.goal_scope import OneGoal
from agent_comms.goal_states import ActiveGoal
from agent_comms.routing import ScheduledTurn
from delivery_owner_fixture import canonical_agent


async def test_reserved_goal_is_unchanged_and_schedules_after_operator_stop(comms, tmp_path):
    owner = canonical_agent(comms, auto_wake=False)
    thread = owner.sessions.declare_thread(str(tmp_path))
    session = thread.name
    await owner.sessions.bind_owned(thread, session)
    try:
        goal = comms.goals.update_goal(thread.name, SetGoalAction(text="Finish the private work"))
        store = comms.goals.open_goal_store()
        generation = store.require_generation(goal.id)
        initial = owner.turns.goals.prepare_goal(session)
        assert isinstance(initial, ScheduledTurn)
        assert initial.goal_id == OneGoal(goal.id)
        permit = store.claim_launch(store.reserve(
            goal.id, generation.number, ready_grant=store.ready_grant(goal.id, generation.number),
        ))
        history = comms.registry.goal_history(thread.name)
        assert isinstance(store.require_generation(goal.id).lifecycle, ReservedGeneration)

        assert owner.turns.goals.prepare_goal(session) is None
        assert comms.registry.require(thread.name).goal == goal
        assert isinstance(comms.registry.require(thread.name).goal.state, ActiveGoal)
        assert comms.registry.goal_history(thread.name) == history

        # The same permit the operator retires; no Retry or goal-state repair.
        store.stopped_permit(goal.id).stop(store)
        assert isinstance(store.require_generation(goal.id).lifecycle, ReadyGeneration)
        scheduled = owner.turns.goals.prepare_goal(session)
        assert isinstance(scheduled, ScheduledTurn)
        assert scheduled.goal_id == OneGoal(goal.id)
        assert comms.registry.require(thread.name).goal == goal
        assert comms.registry.goal_history(thread.name) == history
        assert permit.reservation.generation + 1 == store.require_generation(goal.id).number
    finally:
        await owner.shutdown()
