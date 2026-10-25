"""Native stories use one copied Todo record, including a resent send-back."""


from agent_comms.repository_name import RepositoryName

import time
from dataclasses import replace

import pytest

from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec, MessageId
from agent_comms.goal_actions import CreateProblemGoalAction, OrderGoalAction
from agent_comms.order_story import FleetStory, OrderStory
from agent_comms.recorded_time import EventTime, RecordedEventTime
from agent_comms.todos import (
    AcceptedVerdict, DoneTodoState, EditSendBackVerdict, JudgedCall, OrderMessage, ReleaseTodo,
    SendBackVerdict, StrikeReason,
    StrikeTodo, TodoError, UnorderedProblemOrder, WithdrawTodo,
)
from agent_comms.tool_caller import NoThreadCaller, ThreadCaller
from agent_comms.change_report import FileLine
from agent_comms.errors import MissingSourceLine
from agent_comms.order_tools import CommsChangeProblemTool, CommsFleetStoryTool, CommsOrderStoryTool, CommsTodoCreateTool, CommsOrderTool, CommsVerdictTool, CommsVerdictsTool
from agent_comms.installer.backup import StoreBackup
from test_todos import order
from source_revision_fixture import SourceRepository
from agent_comms.threads import Thread
from agent_comms.registry_document import RegisteredCommanderProject


def board_read(tool, comms, caller, thread="lead"):
    """A board tool's reply names whose board it read; the rest is that board's record."""
    reply = tool.invoke(comms, {"thread": thread}, caller=caller)
    assert reply.pop("board") == thread
    return reply


def test_order_story_copied_bus_strike_and_send_back_once(comms, tmp_path):
    source = SourceRepository.create(tmp_path / 'source')
    for name in ("lead", "worker"):
        comms.registry.declare(Thread(name, frozenset({"comms"}), str(tmp_path)))
    request = comms.goals.todos.add_request(
        comms.registry.require('lead').incarnation, 'Repair the worker').id
    caller = ThreadCaller("lead")
    caller.invoke(CommsTodoCreateTool(owner="worker", order=CreateProblemGoalAction(
        problem="P1", order=replace(order(source), edits=("Repair the existing record owner",)),
        request=request, review_at=RecordedEventTime(1000),
        commander="lead", assigner=comms.registry.require('lead').incarnation,
        repo=RepositoryName.from_wire("org/repo"), text="Repair the worker")), comms)
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
    sent_back = SendBackVerdict(
        todo.revision, actor, todo.held(), "bad-call", StrikeReason(reason),
        evidence=(todo.order.checked().owner,))
    before_send_back = comms.bus.bus.revision()
    caller.invoke(CommsVerdictTool(problem="P1", operation=sent_back), comms)
    assert comms.bus.bus.revision() == before_send_back + 1
    published = comms.bus.bus.lookup(before_send_back + 1)
    assert published.target == "#comms" and published.body.count(reason) == 1
    assert published.includes_lookup(comms.registry.require("worker").incarnation.lookup)
    with pytest.raises(TodoError, match="bad-call: Tool call bad-call already has a verdict on P1"):
        caller.invoke(CommsVerdictTool(problem="P1", operation=sent_back), comms)
    assert comms.bus.bus.revision() == before_send_back + 1
    todo = comms.goals.todos.get("P1")
    caller.invoke(CommsOrderTool(owner="worker", order=OrderGoalAction(
        problem="P1", expected_revision=todo.revision, assigner=actor, commander="lead",
        request=request, review_at=RecordedEventTime(2000),
        order=replace(order(source), id="resent-order"))), comms)
    current = comms.goals.todos.get("P1")
    with pytest.raises(ValueError):
        current.latest_message
    for message_id in map(MessageId.mint, ("initial order", "current order")):
        caller.invoke(CommsChangeProblemTool(problem=current.id, operation=OrderMessage(
            current.revision, actor, message_id)), comms)
        current = comms.goals.todos.get(current.id)
        assert current.latest_message.message_id == message_id
    copied_root = tmp_path / "copy"
    StoreBackup.copy_tree(comms.root, copied_root)
    copied = wire(copied_root)
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
    fleet = FieldCodec.decode(FleetStory, board_read(CommsFleetStoryTool, copied, NoThreadCaller()))
    assert fleet.lines == (f"P1 worker: {saved.order.line}",)
    assert copied.goals.todos.get("P1") == saved == comms.goals.todos.get("P1")
    assert copied.goals.todos.path.stat().st_mtime_ns == stored_at
    with copied.goals.todos.writing() as db:
        for problem in ("P2", "P3"):
            replace(saved, id=problem, assignment=None, order=UnorderedProblemOrder(),
                    strikes=(), verdicts=(), messages=()).insert(db)
    copied.goals.todos.link(actor, "P2", "worker", saved.id)
    linked = FieldCodec.decode(OrderStory, CommsOrderStoryTool.invoke(
        copied, {"order_id": "resent-order"}, caller=caller))
    assert tuple(problem.problem for problem in linked.problems) == ("P1", "P2")
    assert linked.problems[0] == story.problems[0]
    assert linked.line.count(reason) == linked.line.count(standalone) == 1
    linked_fleet = FieldCodec.decode(FleetStory, board_read(CommsFleetStoryTool, copied, caller))
    assert linked_fleet.lines == (
        *fleet.lines, f"P2 worker: {copied.goals.todos.get('P2').order.line}")
    current = copied.goals.todos.get(saved.id)
    previous = current.held()
    released = copied.goals.todos.apply(current.id, ReleaseTodo(
        current.revision, actor, previous,
        StrikeReason("The copied order is released so its original public history can be checked."),
    ))
    withdrawn = copied.goals.todos.apply(current.id, WithdrawTodo(
        released.revision, actor, previous,
        "This copied order is withdrawn after checking its original recorded public events.",
    ))
    assert withdrawn.state is DoneTodoState
    assert FieldCodec.decode(
        FleetStory, board_read(CommsFleetStoryTool, copied, caller)).lines == linked_fleet.lines[1:]
    with pytest.raises(TodoError):
        CommsOrderStoryTool.invoke(copied, {"order_id": "absent-order"}, caller=caller)


def edit_order(comms, tmp_path):
    """An order on one registered repository whose holder edits a second one."""
    source = SourceRepository.create(tmp_path / 'source')
    other = SourceRepository.create(tmp_path / 'other', RepositoryName.from_wire("org/other"))
    for name in ("lead", "worker"):
        comms.registry.declare(Thread(name, frozenset({"comms"}), str(tmp_path)))
    lead = comms.registry.require('lead').incarnation
    request = comms.goals.todos.add_request(lead, 'Repair the worker').id
    caller = ThreadCaller("lead")
    caller.invoke(CommsTodoCreateTool(owner="worker", order=CreateProblemGoalAction(
        problem="P1", order=replace(order(source), edits=("Repair the existing record owner",)),
        request=request, review_at=RecordedEventTime(1000),
        commander="lead", assigner=lead, repo=source.github, text="Repair the worker")), comms)
    # Registered after the order is sent: the edit's repository is found among these.
    comms.registry.set_commander_project(RegisteredCommanderProject(lead, (source, other)))
    (other.checkout / "other_module.py").write_text("first\nsecond\n")
    return caller, other


def sent_back(comms, other, call: str, line: int, at: EventTime) -> JudgedCall:
    todo = comms.goals.todos.get("P1")
    return JudgedCall("P1", EditSendBackVerdict(
        todo.revision, comms.registry.require("lead").incarnation, todo.held(), call,
        StrikeReason("The worker wrote the same marker decision twice in one module."),
        evidence=(FileLine("other_module.py", line),),
        path=str(other.checkout / "other_module.py"), at=at))


def test_one_send_back_over_two_edits_cites_their_checkout_and_strikes_once(comms, tmp_path):
    """The order's check repository holds no other_module.py: the cited line is read from the
    checkout the edits were made in, and two edits judged by one call are one miss."""
    caller, other = edit_order(comms, tmp_path)
    todo = comms.goals.todos.get("P1")
    actor = comms.registry.require("lead").incarnation
    reason = StrikeReason("The worker wrote the same marker decision twice in one module.")
    at = EventTime.capture(time.time())
    with pytest.raises(MissingSourceLine, match="other_module.py:3 does not exist"):
        caller.invoke(CommsVerdictsTool(verdicts=(sent_back(comms, other, "first-call", 3, at),)), comms)
    with pytest.raises(MissingSourceLine, match="other_module.py does not exist at"):
        caller.invoke(CommsVerdictTool(problem="P1", operation=SendBackVerdict(
            todo.revision, actor, todo.held(), "first-call", reason,
            evidence=(FileLine("other_module.py", 2),))), comms)
    caller.invoke(CommsVerdictsTool(verdicts=(sent_back(comms, other, "first-call", 2, at),
                                              sent_back(comms, other, "second-call", 2, at))), comms)
    judged = comms.goals.todos.get("P1")
    assert len(judged.live_strikes("worker")) == 1
    assert {verdict.tool_call_id for verdict in judged.verdicts} >= {"first-call", "second-call"}


def test_two_send_backs_with_the_same_reason_strike_twice(comms, tmp_path):
    """A unit that repeats a miss after it was sent back is struck again, whatever the words."""
    caller, other = edit_order(comms, tmp_path)
    for call in ("first-call", "second-call"):
        caller.invoke(CommsVerdictsTool(verdicts=(
            sent_back(comms, other, call, 2, EventTime.capture(time.time())),)), comms)
    assert len(comms.goals.todos.get("P1").live_strikes("worker")) == 2
