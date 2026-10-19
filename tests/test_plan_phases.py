from dataclasses import replace

from agent_comms.plan_phases import (
    CommanderPagePhase,
    ControlPlanePhase,
    CutoverPhase,
    PlanPhase,
    RecordPlanePhase,
    ReviewQueuePhase,
    SkillPhase,
    StoryPhase,
)
from agent_comms.todos import DoneTodoState
from agent_comms.tools import CommsOrderServeTool

from test_commander_problem_orders import create


def test_cutover_waits_for_native_work_and_views_follow_cutover(commander):
    comms, caller = commander
    required = (RecordPlanePhase, ReviewQueuePhase, ControlPlanePhase)
    phases = (*required, CutoverPhase, StoryPhase, CommanderPagePhase)
    todos = []
    for index, phase in enumerate(phases, 1):
        todo = create(commander, f"P{index}")
        CommsOrderServeTool(problem=todo.id, serves=phase).apply(comms, caller)
        todos.append(comms.goals.todos.get(todo.id))

    native_done = tuple(
        replace(todo, state=DoneTodoState) if todo.order.serves in required else todo
        for todo in todos
    )
    assert CutoverPhase.ready(native_done)
    assert not StoryPhase.ready(native_done)
    assert not CommanderPagePhase.ready(native_done)
    assert not SkillPhase.ready(native_done)

    for unfinished in required:
        snapshot = tuple(
            todo if todo.order.serves in (unfinished, CutoverPhase)
            else replace(todo, state=DoneTodoState)
            for todo in todos
        )
        assert not CutoverPhase.ready(snapshot)

    cutover_done = tuple(
        replace(todo, state=DoneTodoState) if todo.order.serves is CutoverPhase else todo
        for todo in native_done
    )
    assert StoryPhase.ready(cutover_done)
    assert CommanderPagePhase.ready(cutover_done)
    assert SkillPhase.ready(cutover_done)
    assert not any(
        dependency in (StoryPhase, CommanderPagePhase)
        for phase in PlanPhase.members_with(PlanPhase)
        for dependency in phase.after
    )
