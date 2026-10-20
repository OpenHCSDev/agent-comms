"""Native stories use one copied Todo record, including a resent send-back."""
from agent_comms.repository_name import RepositoryName

from dataclasses import replace

import pytest

from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import CreateProblemGoalAction, OrderGoalAction, SetGoalAction
from agent_comms.goals import RequestGoalOrder
from agent_comms.order_story import FleetStory, OrderStory
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.todos import (
    AcceptedVerdict, SendBackVerdict, StrikeReason, StrikeTodo, TodoError, UnorderedProblemOrder,
)
from agent_comms.tool_caller import NoThreadCaller, ThreadCaller
from agent_comms.tools import (
    CommsFleetStoryTool, CommsOrderStoryTool, CommsTodoCreateTool, CommsOrderTool, CommsVerdictTool,
)
from agent_comms.installer.backup import StoreBackup
from test_todos import order


def test_order_story_copied_bus_strike_and_send_back_once(comms, tmp_path):
    comms.messaging.initialize_private_initial_protocol()
    for name in ("lead", "worker"):
        comms.registry.declare(comms.registry.new_thread(name, frozenset(), str(tmp_path)))
    comms.goals.update_goal("lead", SetGoalAction(
        text="Repair the worker", goal_id="request", order=RequestGoalOrder()))
    caller = ThreadCaller("lead")
    caller.invoke(CommsTodoCreateTool(owner="worker", order=CreateProblemGoalAction(
        problem="P1", order=replace(order(tmp_path), edits=("Repair the existing record owner",)),
        request="request", deadline=RecordedEventTime(1000),
        commander="lead", repo=RepositoryName.from_wire("org/repo"), text="Repair the worker")), comms)
    todo = comms.goals.todos.get("P1")
    actor = comms.registry.require("lead").incarnation
    reason = "The worker changed the wrong owner and left the decision duplicated."
    standalone = "The worker omitted the requested application check during the final review."
    comms.goals.todos.apply("P1", StrikeTodo(todo.revision, actor, todo.held(), standalone,
                                                evidence=(todo.order.checked().owner,)))
    todo = comms.goals.todos.get("P1")
    caller.invoke(CommsVerdictTool(problem="P1", operation=AcceptedVerdict(
        todo.revision, actor, todo.held(), "accepted-call", quote="Repair the existing record owner")), comms)
    todo = comms.goals.todos.get("P1")
    caller.invoke(CommsVerdictTool(problem="P1", operation=SendBackVerdict(
        todo.revision, actor, todo.held(), "bad-call", StrikeReason(reason),
        evidence=(todo.order.checked().owner,))), comms)
    todo = comms.goals.todos.get("P1")
    caller.invoke(CommsOrderTool(owner="worker", order=OrderGoalAction(
        problem="P1", expected_revision=todo.revision, assigner=actor, commander="lead",
        request="request", deadline=RecordedEventTime(2000),
        order=replace(order(tmp_path), id="resent-order"))), comms)
    copied_root = tmp_path / "copy"
    StoreBackup.copy_tree(comms.root, copied_root)
    copied = Comms(copied_root)
    saved = copied.goals.todos.get("P1")
    stored_at = copied.goals.todos.path.stat().st_mtime_ns
    result = CommsOrderStoryTool.invoke(copied, {"order_id": "resent-order"}, caller=NoThreadCaller())
    story = FieldCodec.decode(OrderStory, result)
    assert story.line.count(reason) == story.line.count(standalone) == 1
    assert story.line.count("Ordered resent-order:") == story.line.count("Accepted") == 1
    assert story.line.count("bad-call") == story.line.count("accepted-call") == 1
    assert "strike recorded" in story.line
    assert len(saved.strikes) == len(saved.verdicts) == 2
    assert CommsOrderStoryTool.invoke(copied, {"order_id": "resent-order"}, caller=caller) == result
    fleet = FieldCodec.decode(FleetStory, CommsFleetStoryTool.invoke(
        copied, {"thread": "lead"}, caller=NoThreadCaller()))
    assert fleet.lines == (story.line,)
    assert copied.goals.todos.get("P1") == saved == comms.goals.todos.get("P1")
    assert copied.goals.todos.path.stat().st_mtime_ns == stored_at
    with copied.goals.todos.writing() as db:
        for problem in ("P2", "P3"):
            replace(saved, id=problem, assignment=None, order=UnorderedProblemOrder(),
                    strikes=(), verdicts=(), messages=()).insert(db)
    copied.goals.todos.link(actor, "P2", "worker", saved.order.checked().id)
    linked = FieldCodec.decode(OrderStory, CommsOrderStoryTool.invoke(
        copied, {"order_id": "resent-order"}, caller=caller))
    assert tuple(problem.problem for problem in linked.problems) == ("P1", "P2")
    assert linked.problems[0] == story.problems[0]
    assert linked.line.count(reason) == linked.line.count(standalone) == 1
    linked_fleet = FieldCodec.decode(FleetStory, CommsFleetStoryTool.invoke(
        copied, {"thread": "lead"}, caller=caller))
    assert linked_fleet.lines == (linked.line,)
    with pytest.raises(TodoError, match="Unknown saved order"):
        CommsOrderStoryTool.invoke(copied, {"order_id": "absent-order"}, caller=caller)
