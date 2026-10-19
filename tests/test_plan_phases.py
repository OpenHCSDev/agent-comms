from dataclasses import replace

import pytest

from agent_comms.field_codec import FieldCodec
from agent_comms.standing_rules import PlanIdle
from agent_comms.plan_phases import (
    CommanderPagePhase,
    ControlPlanePhase,
    CutoverPhase,
    PlanPhase,
    RecordPlanePhase,
    SkillPhase,
    StoryPhase,
    UnplannedPhase,
)
from agent_comms.todos import (
    DoneTodoState,
    FoldedProblemOrder,
    InstalledProblemOrder,
    ProblemOrder,
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


def test_unordered_findings_have_no_invented_phase_declaration(commander):
    comms, caller = commander
    todo = create(commander, "P1")
    finding = replace(todo, assignment=None, order=UnorderedProblemOrder())
    with comms.goals.todos.writing() as db:
        finding.upsert(db)
    assert finding.order.phase is UnplannedPhase
    assert not RecordPlanePhase.serving((finding,))
    assert [rule.phase for rule in PlanIdle.violations(comms, caller.name)] == [RecordPlanePhase]
    with pytest.raises(ValueError, match="Only a checked order"):
        CommsOrderServeTool(problem=finding.id, serves=StoryPhase).apply(comms, caller)
    assert comms.goals.todos.get(finding.id) == finding


def test_retained_phase_is_derived_and_round_trips_without_a_second_field(commander):
    todo = create(commander, "P1")
    original = todo.order.for_phase(RecordPlanePhase)
    retained = (
        original.merged("a" * 40),
        FoldedProblemOrder(original=original, into="P2", why="Keep one owner"),
    )
    for order in retained:
        saved = replace(todo, order=order, state=DoneTodoState, assignment=None)
        assert RecordPlanePhase.serving((saved,)) == (saved,)
        wire = FieldCodec.encode(order)
        assert "serves" not in wire
        restored = FieldCodec.decode(ProblemOrder, wire)
        assert restored == order and restored.phase is RecordPlanePhase
        changed = replace(order, original=original.for_phase(ControlPlanePhase))
        assert changed.phase is ControlPlanePhase
        assert original.phase is RecordPlanePhase
        assert not order.accepted


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
