"""Native stories retain their append-only events across a copied bus and resend."""


import time
from dataclasses import replace

import pytest

from agent_comms.comms import wire
from agent_comms.errors import MissingSourceLine, TodoError
from agent_comms.installer.backup import StoreBackup
from agent_comms.nominal.field_codec import (
    FieldCodec,
    MessageId,
    OrderId,
    ToolCallId,
)
from agent_comms.planning.goal_actions import (
    CreateProblemGoalAction,
    OrderGoalAction,
)
from agent_comms.planning.order_story import OrderStory, TeamStory
from agent_comms.planning.order_words import StrikeReason
from agent_comms.planning.orders import (
    AcceptedVerdict,
    DoneTodoState,
    EditSendBackVerdict,
    JudgedCall,
    OrderMessage,
    OrderVerdict,
    ReleaseTodo,
    SendBackVerdict,
    StrikeTodo,
    UnorderedProblemOrder,
    WithdrawTodo,
)
from agent_comms.projects.change_report import FileLine
from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.registry.registry_document import RegisteredTeamProject
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.recorded_time import EventTime, RecordedEventTime
from agent_comms.transport.tool_caller import NoThreadCaller, PeerCaller, ThreadCaller
from agent_comms.transport.tools.orders import (
    CommsChangeProblemTool,
    CommsOrderStoryTool,
    CommsOrderTool,
    CommsTeamStoryTool,
    CommsTodoCreateTool,
    CommsVerdictsTool,
    CommsVerdictTool,
)
from source_revision_fixture import SourceRepository
from test_todos import order


def board_read(tool, comms, caller):
    """Every reader reads the one shared board."""
    return tool.invoke(comms, {}, caller=caller)


def test_order_story_copied_bus_strike_and_send_back_once(comms, tmp_path):
    source = SourceRepository.create(tmp_path / 'source')
    for name in ("colleague", "worker"):
        comms.registry.declare(NativeThread(name, frozenset({"comms"}), Checkout(tmp_path)))
    request = comms.goals.todos.add_request(
        comms.registry.require('colleague').incarnation, 'Repair the worker').id
    caller = ThreadCaller("colleague")
    caller.invoke(CommsTodoCreateTool(owner="worker", order=CreateProblemGoalAction(
        problem="P1", order=replace(order(source), edits=("Repair the existing record owner",)),
        request=request, review_at=RecordedEventTime(1000),
        assigner=comms.registry.require('colleague').incarnation,
        repo=RepositoryName.from_wire("org/repo"), text="Repair the worker")), comms)
    todo = comms.goals.todos.get("P1")
    first_order_id = todo.order.checked().id
    actor = comms.registry.require("colleague").incarnation
    reason = "The worker changed the wrong owner and left the decision duplicated."
    standalone = "The worker omitted the requested application check during the final review."
    comms.goals.todos.apply("P1", StrikeTodo(todo.revision, actor, todo.held(), standalone,
                                                evidence=(todo.order.checked().owner,)))
    todo = comms.goals.todos.get("P1")
    caller.invoke(CommsVerdictTool(problem="P1", operation=AcceptedVerdict(
        todo.revision, actor, todo.held(), ToolCallId("accepted-call"), quote="Repair the existing record owner")), comms)
    todo = comms.goals.todos.get("P1")
    sent_back = SendBackVerdict(
        todo.revision, actor, todo.held(), ToolCallId("bad-call"), StrikeReason(reason),
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
    resent_id = OrderId.mint()
    worker = PeerCaller("worker")
    worker.invoke(CommsOrderTool(owner="worker", order=OrderGoalAction(
        problem="P1", expected_revision=todo.revision,
        assigner=comms.registry.require("worker").acting_identity, request=request, review_at=RecordedEventTime(2000),
        order=replace(order(source), id=resent_id))), comms)
    current = comms.goals.todos.get("P1")
    with pytest.raises(ValueError):
        current.latest_message
    for message_id in map(MessageId.mint, ("initial order", "current order")):
        caller.invoke(CommsChangeProblemTool(problem=current.id, operation=OrderMessage(
            current.revision, actor, message_id, at=RecordedEventTime(time.time()))), comms)
        current = comms.goals.todos.get(current.id)
        assert current.latest_message.message_id == message_id
    copied_root = tmp_path / "copy"
    StoreBackup.copy_tree(comms.root, copied_root)
    copied = wire(copied_root)
    saved = copied.goals.todos.get("P1")
    stored_at = copied.goals.todos.path.stat().st_mtime_ns
    result = CommsOrderStoryTool.invoke(copied, {"order_id": str(resent_id)}, caller=NoThreadCaller())
    story = FieldCodec.decode(OrderStory, result)
    archived = FieldCodec.decode(OrderStory, CommsOrderStoryTool.invoke(
        copied, {"order_id": first_order_id}, caller=NoThreadCaller()))
    assert archived.order_id == first_order_id
    assert archived.problems == story.problems
    assert story.line.count(reason) == story.line.count(standalone) == 1
    assert story.line.count(f"Ordered {resent_id}:") == story.line.count("Accepted") == 1
    assert story.line.count("bad-call") == story.line.count("accepted-call") == 1
    assert "strike recorded" in story.line
    assert len(saved.history.events(saved.id, StrikeTodo)) == len(saved.history.events(saved.id, OrderVerdict)) == 2
    assert CommsOrderStoryTool.invoke(copied, {"order_id": str(resent_id)}, caller=caller) == result
    fleet = FieldCodec.decode(TeamStory, board_read(CommsTeamStoryTool, copied, NoThreadCaller()))
    assert fleet.lines == (f"P1 worker: {saved.order.line}",)
    assert copied.goals.todos.get("P1") == saved == comms.goals.todos.get("P1")
    assert copied.goals.todos.path.stat().st_mtime_ns == stored_at
    with copied.goals.todos.writing() as db:
        for problem in ("P2", "P3"):
            replace(saved, id=problem, assignment=None, order=UnorderedProblemOrder()).insert(db)
    copied.goals.todos.link(copied.registry.require("worker").acting_identity, "P2", "worker", saved.id)
    linked = FieldCodec.decode(OrderStory, CommsOrderStoryTool.invoke(
        copied, {"order_id": str(resent_id)}, caller=caller))
    assert tuple(problem.problem for problem in linked.problems) == ("P1", "P2")
    assert linked.problems[0] == story.problems[0]
    assert linked.line.count(reason) == linked.line.count(standalone) == 1
    linked_team = FieldCodec.decode(TeamStory, board_read(CommsTeamStoryTool, copied, caller))
    assert linked_team.lines == (
        *fleet.lines, f"P2 worker: {copied.goals.todos.get('P2').order.line}")
    current = copied.goals.todos.get(saved.id)
    previous = current.held()
    released = copied.goals.todos.apply(current.id, ReleaseTodo(
        current.revision, previous.owner_identity, previous,
        StrikeReason("The copied holder releases its order so original public history can be checked."),
    ))
    withdrawn = copied.goals.todos.apply(current.id, WithdrawTodo(
        released.revision, actor,
        "This copied order is withdrawn after checking its original recorded public events.",
    ))
    assert withdrawn.state is DoneTodoState
    assert FieldCodec.decode(
        TeamStory, board_read(CommsTeamStoryTool, copied, caller)).lines == linked_team.lines[1:]
    with pytest.raises(TodoError):
        CommsOrderStoryTool.invoke(copied, {"order_id": str(OrderId.mint())}, caller=caller)


def edit_order(comms, tmp_path):
    """An order on one registered repository whose holder edits a second one."""
    source = SourceRepository.create(tmp_path / 'source')
    other = SourceRepository.create(tmp_path / 'other', RepositoryName.from_wire("org/other"))
    for name in ("colleague", "worker"):
        comms.registry.declare(NativeThread(name, frozenset({"comms"}), Checkout(tmp_path)))
    colleague = comms.registry.require('colleague').incarnation
    request = comms.goals.todos.add_request(colleague, 'Repair the worker').id
    caller = ThreadCaller("colleague")
    caller.invoke(CommsTodoCreateTool(owner="worker", order=CreateProblemGoalAction(
        problem="P1", order=replace(order(source), edits=("Repair the existing record owner",)),
        request=request, review_at=RecordedEventTime(1000),
        assigner=colleague, repo=source.github, text="Repair the worker")), comms)
    # Registered after the order is sent: the edit's repository is found among these.
    comms.registry.set_team_project(RegisteredTeamProject((source, other)))
    (other.checkout / "other_module.py").write_text("first\nsecond\n")
    return caller, other


def sent_back(comms, other, call: ToolCallId, line: int, at: EventTime) -> JudgedCall:
    todo = comms.goals.todos.get("P1")
    return JudgedCall("P1", EditSendBackVerdict(
        todo.revision, comms.registry.require("colleague").incarnation, todo.held(), call,
        StrikeReason("The worker wrote the same marker decision twice in one module."),
        evidence=(FileLine("other_module.py", line),),
        path=str(other.checkout / "other_module.py"), at=at))


def test_one_send_back_over_two_edits_cites_their_checkout_and_strikes_once(comms, tmp_path):
    """The order's check repository holds no other_module.py: the cited line is read from the
    checkout the edits were made in, and two edits judged by one call are one miss."""
    caller, other = edit_order(comms, tmp_path)
    todo = comms.goals.todos.get("P1")
    actor = comms.registry.require("colleague").incarnation
    reason = StrikeReason("The worker wrote the same marker decision twice in one module.")
    at = EventTime.capture(time.time())
    with pytest.raises(MissingSourceLine, match="other_module.py:3 does not exist"):
        caller.invoke(CommsVerdictsTool(verdicts=(sent_back(comms, other, ToolCallId("first-call"), 3, at),)), comms)
    with pytest.raises(MissingSourceLine, match="other_module.py does not exist at"):
        caller.invoke(CommsVerdictTool(problem="P1", operation=SendBackVerdict(
            todo.revision, actor, todo.held(), ToolCallId("first-call"), reason,
            evidence=(FileLine("other_module.py", 2),))), comms)
    caller.invoke(CommsVerdictsTool(verdicts=(sent_back(comms, other, ToolCallId("first-call"), 2, at),
                                              sent_back(comms, other, ToolCallId("second-call"), 2, at))), comms)
    judged = comms.goals.todos.get("P1")
    assert judged.strikes_against(comms.registry.require("worker").incarnation) == 1
    assert {verdict.tool_call_id for verdict in judged.history.events(judged.id, SendBackVerdict)} >= {
        "first-call", "second-call"}


def test_two_send_backs_with_the_same_reason_strike_twice(comms, tmp_path):
    """A holder that repeats a miss after it was sent back is struck again, whatever the words."""
    caller, other = edit_order(comms, tmp_path)
    for call in map(ToolCallId, ("first-call", "second-call")):
        caller.invoke(CommsVerdictsTool(verdicts=(
            sent_back(comms, other, call, 2, EventTime.capture(time.time())),)), comms)
    assert comms.goals.todos.get("P1").strikes_against(comms.registry.require("worker").incarnation) == 2
