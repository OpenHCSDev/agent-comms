from dataclasses import replace

from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import CreateProblemGoalAction
from agent_comms.plan_phases import ReviewQueuePhase
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.standing_rules import PlanIdle, StandingRule
from agent_comms.todos import Todo
from agent_comms.tools import CommsTodoCreateTool

from test_commander_problem_orders import order


def test_plan_idle_names_a_ready_phase_until_an_order_serving_it_is_open(commander):
    comms, caller = commander

    def idle():
        todos = comms.goals.todos.for_commander(comms.registry.require('lead').incarnation)
        return [rule.phase for rule in StandingRule.broken(todos) if isinstance(rule, PlanIdle)]

    assert ReviewQueuePhase in idle()
    assert PlanIdle(ReviewQueuePhase).line() == (
        'PLAN IDLE: phase review_queue ready, no open order -> dispatch it this wake')
    CommsTodoCreateTool(owner='worker', order=CreateProblemGoalAction(
        problem='P1', order=replace(order(comms.root, 'order-P1'), serves=ReviewQueuePhase),
        request='request', deadline=RecordedEventTime(1000), commander='lead',
        repo='OpenHCSDev/agent-comms', text='Build the review queue',
    )).apply(comms, caller)
    stored = comms.goals.todos.get('P1')
    assert stored.order.serves is ReviewQueuePhase
    assert FieldCodec.decode(Todo, FieldCodec.encode(stored)) == stored
    assert ReviewQueuePhase not in idle()
