from dataclasses import replace

from agent_comms.plan_phases import (
    CommanderPagePhase,
    ControlPlanePhase,
    CutoverPhase,
    PlanPhase,
    RecordPlanePhase,
    SkillPhase,
    StoryPhase,
)
from agent_comms.todos import (
    DoneTodoState,
    InstalledProblemOrder,
    OperationDelivery,
    UnorderedProblemOrder,
    WithdrawnProblemOrder,
)
from agent_comms.tools import CommsOrderServeTool
from agent_comms.work_dispatch import CheckedPhaseTask

from test_commander_problem_orders import create


def accepted(todo):
    installed = todo.order.copy_as(
        InstalledProblemOrder, delivery=OperationDelivery(),
        works="The declared whole phase worked through its acceptance run.",
        artifact="phase-acceptance.txt",
    )
    return replace(todo, state=DoneTodoState, assignment=None, order=installed)


def task(todo):
    return CheckedPhaseTask(
        problem=todo.id, repo=todo.repo, text=todo.text,
        order=todo.order, deadline_seconds=300,
    )


def test_views_wait_for_native_work_but_never_block_cutover(commander, monkeypatch):
    comms, caller = commander
    # ReviewQueuePhase is explicitly delivered as #1654, not inferred from a subtask.
    required = (RecordPlanePhase, ControlPlanePhase)
    phases = (*required, CutoverPhase, StoryPhase, CommanderPagePhase)
    todos = []
    for index, phase in enumerate(phases, 1):
        todo = create(commander, f"P{index}")
        CommsOrderServeTool(problem=todo.id, serves=phase).apply(comms, caller)
        todo = comms.goals.todos.get(todo.id)
        monkeypatch.setattr(phase, "task", task(todo))
        todos.append(todo)

    record_done = tuple(
        accepted(todo) if todo.order.serves is RecordPlanePhase else todo
        for todo in todos
    )
    assert StoryPhase.ready(record_done)
    assert not CommanderPagePhase.ready(record_done)

    native_done = tuple(
        accepted(todo) if todo.order.serves in required else todo
        for todo in todos
    )
    assert CutoverPhase.ready(native_done)
    assert StoryPhase.ready(native_done)
    assert CommanderPagePhase.ready(native_done)
    assert not SkillPhase.ready(native_done)

    for unfinished in required:
        snapshot = tuple(
            todo if todo.order.serves in (unfinished, CutoverPhase) else accepted(todo)
            for todo in todos
        )
        assert not CutoverPhase.ready(snapshot)

    cutover_done = tuple(
        accepted(todo) if todo.order.serves is CutoverPhase else todo
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


def test_closed_related_subtask_does_not_accept_a_whole_phase(commander, monkeypatch):
    comms, caller = commander
    subtask = create(commander, "P149")
    CommsOrderServeTool(problem=subtask.id, serves=StoryPhase).apply(comms, caller)
    subtask = accepted(comms.goals.todos.get(subtask.id))
    assert not StoryPhase.done((subtask,))

    whole = create(commander, "P999")
    CommsOrderServeTool(problem=whole.id, serves=StoryPhase).apply(comms, caller)
    whole = comms.goals.todos.get(whole.id)
    monkeypatch.setattr(StoryPhase, "task", task(whole))
    assert not StoryPhase.done((subtask, whole))
    assert StoryPhase.done((subtask, accepted(whole)))

    withdrawn = whole.order.copy_as(
        WithdrawnProblemOrder, previous=whole.held(), why="The order was sent in error.",
    )
    assert not StoryPhase.done((replace(whole, state=DoneTodoState, order=withdrawn),))
    assert not UnorderedProblemOrder().accepted
    wrong_phase = replace(accepted(whole), order=replace(accepted(whole).order, serves=ControlPlanePhase))
    assert not StoryPhase.done((wrong_phase,))
