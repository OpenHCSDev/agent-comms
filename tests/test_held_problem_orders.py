"""Problem ids select held orders through real commander commands on copied state."""
from contextlib import chdir
from dataclasses import replace
import json
import shlex
import time

import pytest

from test_todos import order, pr_commander, thread

from agent_comms.commander.order import OrderFile, main
from agent_comms.commander.request import RequestAddVerb
from agent_comms.field_codec import FieldCodec
from agent_comms.installer.practice import practice_copy
from agent_comms.goals import OrderCheck
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.todos import DeliveredProblemOrder, GoalRef, OrderMessage
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import CommsChannelsTool


pytest_plugins = ("test_todos",)


@pytest.fixture
def held_commander(pr_commander):
    source = pr_commander
    worker = thread("worker", source.root, 2.0)
    source.comms.registry.register(worker)
    RequestAddVerb(text="Repair the copied order workflow without changing original retained state").run(source)
    lead = source.comms.registry.require(source.commander)
    todo = source.comms.goals.todos.create(
        "P1", source.repos[0].github, "Repair the held workflow", creator=lead,
        owner=worker, generation="initial-order", goal=GoalRef(lead.name, lead.created_at, lead.goal.require().id),
        review_at=RecordedEventTime(time.time() + 1000), order=order(source.repos[0]),
    )
    published = source.comms.messaging.send(source.commander, worker.name, "Repair the held workflow through its existing declaration.")
    source.comms.goals.todos.apply(todo.id, OrderMessage(todo.revision, lead.incarnation, published.message.message_id))
    original = source.comms.goals.todos.get(todo.id)
    with practice_copy(source.root, source.root.parent / f"{source.root.name}-copied") as (root, _):
        ctx = source.model_copy(update={"root": root, "state": root / "state"})
        yield ctx
    assert source.comms.goals.todos.get(todo.id) == original


def test_clean_resend_derives_the_existing_order_and_leaves_no_strike(held_commander):
    ctx = held_commander
    original = ctx.comms.goals.todos.get("P1")
    marker = ctx.root / "unfinished"
    marker.touch()
    now = time.time()
    replacement = replace(
        order(ctx.repos[0]), id="replacement-order",
        check=OrderCheck(str(ctx.repos[0].checkout), f"test ! -e {shlex.quote(str(marker))}"),
        at=RecordedEventTime(now), first_call_deadline=RecordedEventTime(now + 120),
    )
    document = OrderFile(
        replacement,
        "The copied workflow still contains an unfinished decision that prevents completion",
        "This is stupid because a stale decision prevents the existing owner from finishing",
        unplanned_reason="This isolated workflow verifies held order selection rather than advancing the production plan.",
        problems=("P1",),
    )
    path = ctx.root / "order.json"
    path.write_text(json.dumps(FieldCodec.encode(document)))
    main(["send", "worker", str(path), "--clean"], ctx)
    saved = ctx.comms.goals.todos.get("P1")
    assert saved.order.id != original.order.id
    assert saved.messages[-1].message_id != original.messages[-1].message_id
    assert saved.strikes == original.strikes == ()
    assert ctx.comms.registry.require("worker").goal.require().id == saved.held().generation
    ThreadCaller("worker").invoke(CommsChannelsTool(), ctx.comms)


def test_second_unplanned_order_is_refused_without_recording(held_commander):
    ctx = held_commander
    original = ctx.comms.goals.todos.get("P1")
    marker = ctx.root / "unfinished"
    marker.touch()
    document = OrderFile(
        replace(order(ctx.repos[0]), id="second-order",
                check=OrderCheck(str(ctx.repos[0].checkout), f"test ! -e {shlex.quote(str(marker))}")),
        "The copied workflow still contains an unfinished decision that prevents completion",
        "This is stupid because assigning another order hides the already unfinished work",
        unplanned_reason="This isolated workflow verifies refusal rather than advancing the production plan.",
        problems=("P2",),
    )
    lead = ctx.comms.registry.require(ctx.commander)
    ctx.comms.goals.todos.create(
        "P2", ctx.repos[0].github, "Repair the second workflow", creator=lead, owner=lead,
        generation="second-workflow", goal=GoalRef(lead.name, lead.created_at, lead.goal.require().id),
        review_at=RecordedEventTime(time.time() + 1000), order=document.order,
    )
    path = ctx.root / "second-order.json"
    path.write_text(json.dumps(FieldCodec.encode(document)))
    before = ctx.comms.goals.todos.for_commander(ctx.incarnation)
    goal = ctx.comms.registry.require("worker").goal
    with pytest.raises(SystemExit) as refused:
        main(["send", "worker", str(path)], ctx)
    assert "worker already holds orders P1; running units holding no order:" in str(refused.value)
    assert ctx.comms.goals.todos.for_commander(ctx.incarnation) == before
    assert ctx.comms.goals.todos.get("P1") == original
    assert ctx.comms.registry.require("worker").goal == goal
    print(str(refused.value))


def test_done_problem_delivers_the_units_held_order(held_commander):
    ctx = held_commander
    sha = ctx.repos[0].git(ctx.repos[0].checkout, "rev-parse", "HEAD").strip()
    with chdir(ctx.repos[0].checkout):
        main(["done", "worker", "P1", "--sha", sha], ctx)
    saved = ctx.comms.goals.todos.get("P1")
    assert not saved.pending
    assert isinstance(saved.order, DeliveredProblemOrder)
    assert saved.order.delivery.delivered == sha


def test_a_problem_not_held_by_the_unit_is_refused(held_commander):
    ctx = held_commander
    original = ctx.comms.goals.todos.get("P1")
    with pytest.raises(ValueError, match="worker holds no order on P2"):
        main(["done", "worker", "P2", "--sha", "HEAD"], ctx)
    with pytest.raises(ValueError, match="other holds no order on P1"):
        ctx.comms.goals.todos.resolve_order(ctx.incarnation, "other", "P1")
    assert ctx.comms.goals.todos.get("P1") == original
