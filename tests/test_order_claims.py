"""A holder claims through the real command/tool boundary on a copied bus."""
from contextlib import chdir

import pytest

from agent_comms.commander.order import main
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import SetGoalAction
from agent_comms.goals import ProblemGoalOrder
from agent_comms.todos import (
    ClaimedProblemOrder,
    DeliveredProblemOrder,
    PendingProblemOrder,
    SendBackVerdict,
    StrikeReason,
    TodoConflict,
    TodoError,
    Todo,
)
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import (
    CommsHistoryTool, CommsOrderDeliverTool, CommsVerdictTool, TodoChanged,
)
from test_held_problem_orders import (  # noqa: F401 - shared copied-bus fixture family
    existing_runtime,
    held_commander,
    pr_commander,
)


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

    main(["deliver", "P1", "--fork", "worker", "--pr", "1", "--sha", sha,
          "--report-file", str(report)], ctx)
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
    assert "Claimed by the unit" in ctx.board_text()
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
