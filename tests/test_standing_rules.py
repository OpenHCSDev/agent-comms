from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import CreateProblemGoalAction
from agent_comms.plan_phases import RecordPlanePhase, ReviewQueuePhase, UnplannedPhase
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.standing_rules import PlanIdle, StandingRule
from agent_comms.todos import Todo
from agent_comms.tools import CommsOrderServeTool, CommsTodoCreateTool

from test_commander_problem_orders import order


def test_plan_idle_names_a_ready_phase_until_an_order_serving_it_is_open(commander):
    comms, caller = commander

    def idle():
        todos = comms.goals.todos.for_commander(comms.registry.require('lead').incarnation)
        return [rule.phase for rule in StandingRule.broken(todos) if isinstance(rule, PlanIdle)]

    assert RecordPlanePhase in idle() and ReviewQueuePhase not in idle()
    assert PlanIdle(RecordPlanePhase).line() == (
        'PLAN IDLE: phase record_plane ready, no open order -> dispatch it this wake')
    CommsTodoCreateTool(owner='worker', order=CreateProblemGoalAction(
        problem='P1', order=order(comms.root, 'order-P1'),
        request='request', deadline=RecordedEventTime(1000), commander='lead',
        repo='OpenHCSDev/agent-comms', text='Build the record plane',
    )).apply(comms, caller)
    assert comms.goals.todos.get('P1').order.serves is UnplannedPhase
    assert RecordPlanePhase in idle()
    CommsOrderServeTool(problem='P1', serves=RecordPlanePhase).apply(comms, caller)
    stored = comms.goals.todos.get('P1')
    assert stored.order.serves is RecordPlanePhase
    assert FieldCodec.decode(Todo, FieldCodec.encode(stored)) == stored
    assert RecordPlanePhase not in idle()
