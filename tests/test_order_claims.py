"""A holder claims through the real tool boundary on a copied bus."""
from contextlib import chdir
from dataclasses import replace
import json
import shlex
import time

import pytest

from agent_comms.commander.order import OrderFile, main
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import SetGoalAction
from agent_comms.goals import OrderCheck, ProblemGoalOrder
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.todos import (
    ClaimedProblemOrder,
    CorrectCheckTodo,
    DoneTodoState,
    FailedCheckStrike,
    DeliveredProblemOrder,
    PendingProblemOrder,
    SendBackVerdict,
    StrikeReason,
    StrikeTodo,
    TodoConflict,
    TodoError,
    Todo,
)
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import (
    CommsHistoryTool, CommsHudTool, CommsOrderDeliverTool, CommsVerdictTool, TodoChanged,
)
from test_todos import order, thread

pytest_plugins = ("test_held_problem_orders",)


def test_claim_verdict_family_on_retained_copy(held_commander):
    ctx = held_commander
    comms = ctx.comms
    original = comms.goals.todos.get("P1")
    comms.goals.update_goal("worker", SetGoalAction(
        text=ProblemGoalOrder.text, order=ProblemGoalOrder(),
        goal_id=original.held().generation,
    ))
    sha = ctx.repos[0].git(ctx.repos[0].checkout, "rev-parse", "HEAD").strip()
    report = ctx.root / "report.txt"
    report.write_text("The source change is published; inspect its actual diff before accepting it.")
    request = CommsOrderDeliverTool(
        problem="P1", pull_request=1, head=sha, report=report.read_text(),
    )
    with pytest.raises(TodoConflict):
        ThreadCaller(ctx.commander).invoke(request, comms)
    assert comms.goals.todos.get("P1") == original

    ThreadCaller("worker").invoke(request, comms)
    claimed = comms.goals.todos.get("P1")
    assert isinstance(claimed.order, ClaimedProblemOrder)
    assert not claimed.pending
    assert FieldCodec.decode(Todo, FieldCodec.encode(claimed)) == claimed
    assert claimed.order.claim.head == sha
    assert claimed.order.claim.message.body == report.read_text()
    history = ThreadCaller(ctx.commander).invoke(CommsHistoryTool(peer="worker"), comms)
    sent = next(message for message in history.messages
                if message.message_id == claimed.order.claim.message.message_id)
    assert sent.sender == "worker" and sent.target == ctx.commander
    assert sent.answers and sent.answers == claimed.order.claim.message.answers
    assert "Claimed by the unit" in "\n".join(ctx.call(CommsHudTool()).frame)
    with pytest.raises(TodoError, match="Only a pending order"):
        ThreadCaller("worker").invoke(request, comms)
    assert comms.goals.todos.get("P1") == claimed

    why = StrikeReason(
        "This is stupid because the claimed change leaves the original duplicated decision in place."
    )
    ThreadCaller(ctx.commander).invoke(CommsVerdictTool(
        problem="P1", operation=SendBackVerdict(
            claimed.revision, ctx.incarnation, claimed.held(), "fixture-call", why,
            evidence=(claimed.order.owner,),
        ),
    ), comms)
    pending = comms.goals.todos.get("P1")
    assert isinstance(pending.order, PendingProblemOrder)
    assert pending.order.id != claimed.order.id
    assert why.text in pending.order.edits
    assert claimed.order in pending.prior_orders
    assert pending.strike_count == claimed.strike_count + 1
    response = CommsOrderDeliverTool.invoke(comms, request.arguments, caller=ThreadCaller("worker"))
    assert isinstance(FieldCodec.decode(TodoChanged, response).todo.order, ClaimedProblemOrder)
    with chdir(ctx.repos[0].checkout):
        main(["done", "worker", "P1", "--sha", sha], ctx)
    delivered = comms.goals.todos.get("P1")
    assert isinstance(delivered.order, DeliveredProblemOrder)
    assert delivered.order.delivery.delivered == sha
    assert not delivered.pending


def test_failed_check_strike_releases_at_the_updated_revision(held_commander):
    ctx = held_commander
    store = ctx.comms.goals.todos
    original = store.get("P1")
    first = store.apply(original.id, StrikeTodo(
        original.revision, ctx.incarnation, original.held(),
        "The first review found the original duplicated decision still present.",
        evidence=(original.order.checked().owner,),
    ))
    failure = FailedCheckStrike.make(
        first, time.time(),
        "The delivered change still fails the source check that the order explicitly requires.",
    )

    released = store.apply(first.id, failure)
    assert released.assignment is None
    assert released.revision == first.revision + 2
    assert released.strikes == (*first.strikes, failure)
    assert released.released[-1].expected_revision == first.revision + 1
    with pytest.raises(TodoConflict):
        store.apply(first.id, failure)
    assert store.get(first.id) == released


def claimed_by_worker(ctx) -> Todo:
    ctx.comms.goals.update_goal("worker", SetGoalAction(
        text=ProblemGoalOrder.text, order=ProblemGoalOrder(),
        goal_id=ctx.comms.goals.todos.get("P1").held().generation,
    ))
    sha = ctx.repos[0].git(ctx.repos[0].checkout, "rev-parse", "HEAD").strip()
    report = ctx.root / "report.txt"
    report.write_text("The source change is published; inspect its actual diff before accepting it.")
    ThreadCaller("worker").invoke(CommsOrderDeliverTool(
        problem="P1", pull_request=1, head=sha, report=report.read_text(),
    ), ctx.comms)
    claimed = ctx.comms.goals.todos.get("P1")
    assert isinstance(claimed.order, ClaimedProblemOrder)
    return claimed


def corrected_order_file(ctx) -> str:
    marker = ctx.root / "duplicated-decision"
    marker.touch()
    now = time.time()
    document = OrderFile(
        replace(order(ctx.repos[0]), id="corrected-order",
                check=OrderCheck(str(ctx.repos[0].checkout), f"test ! -e {shlex.quote(str(marker))}"),
                at=RecordedEventTime(now), first_call_deadline=RecordedEventTime(now + 120)),
        "The claimed change keeps the duplicated decision the order told the unit to delete",
        "This is stupid because the commander's order named the wrong member to delete",
        unplanned_reason="This isolated workflow verifies claimed order correction rather than advancing a plan.",
        problems=("P1",),
    )
    path = ctx.root / "corrected-order.json"
    path.write_text(json.dumps(FieldCodec.encode(document)))
    return str(path)


def test_corrected_order_to_the_claimant_replaces_its_claim(held_commander):
    ctx = held_commander
    claimed = claimed_by_worker(ctx)
    main(["send", "worker", corrected_order_file(ctx), "--clean"], ctx)
    corrected = ctx.comms.goals.todos.get("P1")
    assert isinstance(corrected.order, PendingProblemOrder)
    assert corrected.order.id != claimed.order.id
    assert claimed.order in corrected.prior_orders
    assert corrected.holder == "worker"
    assert corrected.strike_count == claimed.strike_count


def test_claimed_order_to_another_unit_is_refused_and_writes_nothing(held_commander):
    ctx = held_commander
    ctx.comms.registry.register(thread("other", ctx.root, 3.0))
    claimed = claimed_by_worker(ctx)
    with pytest.raises(TodoError, match="Only an undelivered order can change owner"):
        main(["send", "other", corrected_order_file(ctx), "--clean", "--reassign",
              "the claimant reported the delivery, so moving it would discard that claim"], ctx)
    assert ctx.comms.goals.todos.get("P1") == claimed


def test_claimed_order_takes_a_check_correction_through_the_real_verb(held_commander):
    ctx = held_commander
    claimed = claimed_by_worker(ctx)
    repo = ctx.repos[0]

    main(["check", "P1", "--check", f"{repo.checkout} :: printf 1 -> 0",
          "--why", "the recorded check also counted lines the order never asked to remove"], ctx)
    corrected = ctx.comms.goals.todos.get("P1")
    assert isinstance(corrected.order, ClaimedProblemOrder)
    assert corrected.order.check.command == "printf 1"
    assert corrected.order.check_corrections[-1].previous == claimed.order.check
    assert (corrected.order.id, corrected.order.claim) == (claimed.order.id, claimed.order.claim)
    assert (corrected.holder, corrected.review_at, corrected.strike_count) == (
        claimed.holder, claimed.review_at, claimed.strike_count)


def test_closed_problem_refuses_a_check_correction_and_writes_nothing(held_commander):
    ctx = held_commander
    claimed = claimed_by_worker(ctx)
    closed = replace(claimed, state=DoneTodoState)
    correction = CorrectCheckTodo(
        expected_revision=claimed.revision, actor=ctx.incarnation,
        previous=claimed.order.check, check=claimed.order.check,
        reason="the recorded check also counted lines the order never asked to remove",
    )
    with pytest.raises(TodoError, match="is done"):
        correction.apply(closed)
    assert ctx.comms.goals.todos.get("P1") == claimed
