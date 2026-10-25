"""A holder claims through the real tool boundary on a copied bus."""
from contextlib import chdir
from dataclasses import replace
import json
import shlex
import time

import pytest

from agent_comms.commander.order import OrderFile, main
from agent_comms.field_codec import FieldCodec, GoalId, RecordProblems
from agent_comms.goal_actions import SetGoalAction
from agent_comms.commander.package import CommanderPackage
from agent_comms.errors import TodoConflict, TodoError, ToolRefusedError
from agent_comms.goals import OrderCheck, ProblemGoalOrder
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.todos import (
    ClaimOrder,
    ClaimedProblemOrder,
    CorrectCheckTodo,
    DoneTodoState,
    FailedCheckStrike,
    DeliveredProblemOrder,
    DeliveryReport,
    PendingProblemOrder,
    OrderClaim,
    ReleaseTodo,
    SendBackVerdict,
    StrikeReason,
    StrikeTodo,
    Todo,
)
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import CommsHistoryTool
from agent_comms.order_runs import ApplicationOrderRun
from agent_comms.order_tools import CommsHudTool, CommsOrderDeliverTool, CommsVerdictTool, TodoChanged
from test_held_problem_orders import merge_two_commit_fix
from test_todos import order, thread

pytest_plugins = ("test_held_problem_orders",)


def test_delivery_report_refuses_a_flat_status_memo():
    with pytest.raises(ToolRefusedError, match="name the slop you killed"):
        DeliveryReport("Ready at abc; check 13 -> 0; tests pass")
    blunt = "Killed the string slop at turn_watchdog.py:137; the typed state answers it now."
    assert DeliveryReport(blunt).text == blunt


def test_work_instructions_carry_the_owner_voice(source_repository):
    heading = CommanderPackage.voice().read_text(encoding="utf-8").splitlines()[0]
    assert any(line.startswith(heading) for line in order(source_repository).work_instructions())


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
    report.write_text("Killed the broken count copy; inspect its actual diff before accepting it.")
    request = CommsOrderDeliverTool(
        problem="P1", pull_request=1, head=sha, report=DeliveryReport(report.read_text()),
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
        main(["done", "worker", "P1"], ctx)
    delivered = comms.goals.todos.get("P1")
    assert isinstance(delivered.order, DeliveredProblemOrder)
    assert delivered.order.delivery.delivered == delivered.order.delivery.commit
    assert not delivered.pending


def test_claimed_check_correction_reads_the_base_the_fix_was_built_on(
    held_commander, cited_source, tmp_path,
):
    """Once the claimed PR merges, current main passes every correct check; the claim's own
    branch point still holds the defect, so a correct check is accepted and done records it."""
    ctx = held_commander
    comms = ctx.comms
    base, first, merge = merge_two_commit_fix(cited_source, tmp_path / "bin", 4)
    original = comms.goals.todos.get("P1")
    comms.goals.update_goal("worker", SetGoalAction(
        text=ProblemGoalOrder.text, order=ProblemGoalOrder(),
        goal_id=original.held().generation,
    ))
    head = cited_source.git(cited_source.checkout, "rev-parse", "fix/4").strip()
    ThreadCaller("worker").invoke(CommsOrderDeliverTool(
        problem="P1", pull_request=4, head=head,
        report=DeliveryReport("The wrong count is dead and merged; check the merged tree."),
    ), comms)
    claimed = comms.goals.todos.get("P1")
    assert claimed.order.check_base(cited_source.checkout) == base
    with chdir(cited_source.checkout):
        main(["check", "P1", "--check", f"{cited_source.checkout} :: cat count.txt -> 0",
              "--why", "The old check counted a legitimate use outside the fixed file"], ctx)
        main(["done", "worker", "P1"], ctx)
    delivered = comms.goals.todos.get("P1")
    assert isinstance(delivered.order, DeliveredProblemOrder)
    assert delivered.order.check.command == "cat count.txt"
    assert (delivered.order.delivery.delivered, delivered.order.delivery.commit) == (first, merge)
    assert delivered.strike_count == claimed.strike_count


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


def test_second_commander_strike_takes_the_task_away(held_commander):
    ctx = held_commander
    store = ctx.comms.goals.todos
    todo = store.get("P1")
    for reason in ("The first review found the original duplicated decision still present.",
                   "The second review found the same duplicated decision restated in a new file."):
        todo = store.apply(todo.id, StrikeTodo(
            todo.revision, ctx.incarnation, todo.held(), reason,
            evidence=(todo.order.checked().owner,),
        ))
    assert todo.holder is None
    assert len(todo.strikes) == 2
    assert todo.released[-1].previous == todo.strikes[-1].previous


def test_unstrike_below_the_limit_gives_the_task_back(held_commander):
    """Two strikes take the task away; voiding the one recorded in error restores its holder
    in the same write."""
    ctx = held_commander
    store = ctx.comms.goals.todos
    todo = store.get("P1")
    holder = todo.held()
    for reason in ("The first review found the original duplicated decision still present.",
                   "The same review counted the duplicated decision a second time in error."):
        todo = store.apply(todo.id, StrikeTodo(
            todo.revision, ctx.incarnation, todo.held(), reason,
            evidence=(todo.order.checked().owner,),
        ))
    assert todo.holder is None
    main(["unstrike", "P1", "--why", "The second strike counted the same duplicated decision from the first review twice."], ctx)
    restored = store.get("P1")
    assert restored.held() == holder
    assert len(restored.live_strikes()) == 1
    assert restored.released == todo.released


def test_unstrike_does_not_undo_an_explicit_release(held_commander):
    ctx = held_commander
    store = ctx.comms.goals.todos
    todo = store.get('P1')
    struck = store.apply(todo.id, StrikeTodo(
        todo.revision, ctx.incarnation, todo.held(),
        'The first review counted a duplicated decision that the source did not contain.',
        evidence=(todo.order.checked().owner,)))
    released = store.apply(todo.id, ReleaseTodo(
        struck.revision, ctx.incarnation, struck.held(),
        StrikeReason('The commander deliberately moved the problem off this unit for independent work.')))
    main(['unstrike', 'P1', '--why',
          'The cited duplicated decision was absent and this strike was recorded in error.'], ctx)
    after = store.get(todo.id)
    assert after.holder is None
    assert not after.live_strikes()
    assert after.released == released.released


def claimed_by_worker(ctx) -> Todo:
    ctx.comms.goals.update_goal("worker", SetGoalAction(
        text=ProblemGoalOrder.text, order=ProblemGoalOrder(),
        goal_id=ctx.comms.goals.todos.get("P1").held().generation,
    ))
    sha = ctx.repos[0].git(ctx.repos[0].checkout, "rev-parse", "HEAD").strip()
    report = ctx.root / "report.txt"
    report.write_text("Killed the broken count copy; inspect its actual diff before accepting it.")
    ThreadCaller("worker").invoke(CommsOrderDeliverTool(
        problem="P1", pull_request=1, head=sha, report=DeliveryReport(report.read_text()),
    ), ctx.comms)
    claimed = ctx.comms.goals.todos.get("P1")
    assert isinstance(claimed.order, ClaimedProblemOrder)
    return claimed


def corrected_order_file(ctx, source) -> str:
    marker = ctx.root / "duplicated-decision"
    marker.touch()
    now = time.time()
    document = OrderFile(
        replace(order(source), id="corrected-order",
                check=OrderCheck(ctx.repos[0].checkout, f"test ! -e {shlex.quote(str(marker))}"),
                at=RecordedEventTime(now), first_call_deadline=RecordedEventTime(now + 120)),
        "The claimed change keeps the duplicated decision the order told the unit to delete",
        "This is stupid because the commander's order named the wrong member to delete",
        unplanned_reason="This isolated workflow verifies claimed order correction rather than advancing a plan.",
        problems=("P1",),
    )
    path = ctx.root / "corrected-order.json"
    path.write_text(json.dumps(FieldCodec.encode(document)))
    return str(path)


def test_corrected_order_to_the_claimant_replaces_its_claim(held_commander, cited_source):
    ctx = held_commander
    claimed = claimed_by_worker(ctx)
    main(["send", "worker", corrected_order_file(ctx, cited_source), "--clean"], ctx)
    corrected = ctx.comms.goals.todos.get("P1")
    assert isinstance(corrected.order, PendingProblemOrder)
    assert corrected.order.id != claimed.order.id
    assert claimed.order in corrected.prior_orders
    assert corrected.holder == "worker"
    assert corrected.strike_count == claimed.strike_count


def test_two_delivery_claims_warn_without_deadlocking_other_orders(
    held_commander, cited_source, capsys,
):
    other_order_goal_id = GoalId.mint()
    ctx = held_commander
    comms = ctx.comms
    store = comms.goals.todos
    source = ctx.repos[0]
    checkout = ctx.root.parent / "other-claim-checkout"
    source.git(source.checkout, "worktree", "add", "-q", "--detach", str(checkout))
    other = thread("other", checkout, 3.0)
    comms.registry.register(other)
    comms.registry.register(thread("third", ctx.root.parent / "third", 4.0))
    lead = comms.registry.require(ctx.commander)
    original = store.get("P1")
    second = store.create(
        "P2", original.repo, "Repair the other held workflow", creator=lead,
        assigner=ctx.incarnation, owner=other, generation=other_order_goal_id,
        request=original.request.require(), review_at=RecordedEventTime(time.time() + 1000),
        order=replace(original.order.checked(), id="other-order"),
    )
    head = source.git(source.checkout, "rev-parse", "HEAD").strip()
    claims = []
    for todo in (original, second):
        holder = comms.registry.require(todo.holder)
        sent = comms.messaging.send(
            ctx.commander, holder.name, todo.order.instructions(todo),
        ).message
        claim = OrderClaim.for_order(
            todo, holder.incarnation, 1, head, "The source repair is published for review.",
            sent.reference, store=store,
        )
        claim = replace(claim, message=replace(claim.message, timestamp=time.time() - 3600))
        claims.append(ClaimOrder(
            todo.revision, holder.incarnation, todo.held().generation, claim,
        ).record(comms.goals, todo.id))

    reply = ctx.root / "claim-status.txt"
    reply.write_text("Waiting on the review, owner lead.")
    capsys.readouterr()
    main(["say", "third", str(reply)], ctx)
    warning = capsys.readouterr().out
    assert "WARNING: DELIVERY AWAITING JUDGEMENT worker P1" in warning
    assert "WARNING: DELIVERY AWAITING JUDGEMENT other P2" in warning
    assert store.get("P1") == claims[0]
    assert store.get("P2") == claims[1]
    with pytest.raises(RecordProblems, match="DELIVERY AWAITING JUDGEMENT worker P1"):
        main(["say", "worker", str(reply), "--answers",
              claims[0].order.claim.message.message_id], ctx)
    assert store.get("P1") == claims[0]
    ask = comms.messaging.send("worker", ctx.commander, "Which owner do I wait on?").message
    main(["say", "worker", str(reply), "--answers", ask.message_id], ctx)
    assert store.get("P1") == claims[0]

    main(["send", "worker", corrected_order_file(ctx, cited_source), "--clean"], ctx)
    corrected = store.get("P1")
    assert isinstance(corrected.order, PendingProblemOrder)
    assert corrected.order.commander_claims() == ()
    assert claims[0].order in corrected.prior_orders
    assert store.get("P2") == claims[1]


def test_claimed_order_to_another_unit_is_refused_and_writes_nothing(held_commander, cited_source):
    ctx = held_commander
    ctx.comms.registry.register(thread("other", ctx.root, 3.0))
    claimed = claimed_by_worker(ctx)
    with pytest.raises(TodoError, match="Only an undelivered order can change owner"):
        main(["send", "other", corrected_order_file(ctx, cited_source), "--clean", "--reassign",
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
    assert corrected.order.corrections[-1].previous == claimed.order.check
    assert (corrected.order.id, corrected.order.claim) == (claimed.order.id, claimed.order.claim)
    assert (corrected.holder, corrected.review_at, corrected.strike_count) == (
        claimed.holder, claimed.review_at, claimed.strike_count)


def test_held_application_order_takes_a_run_correction_through_the_real_verb(held_commander):
    ctx = held_commander
    store = ctx.comms.goals.todos
    current = store.get("P1")
    stale = ApplicationOrderRun("The turn view shows the unit's newest turn after it ends.")
    with store.writing() as connection:
        replace(current, order=replace(current.order, run=stale),
                revision=current.revision + 1).upsert(connection)
    held = store.get("P1")

    main(["run", "P1", "--run", "The order story shows the unit's newest tool call after it ends.",
          "--why", "the recorded run names a turn view that a merged deletion removed"], ctx)
    corrected = store.get("P1")
    assert corrected.order.run == ApplicationOrderRun(
        "The order story shows the unit's newest tool call after it ends.")
    assert corrected.order.corrections[-1].previous == stale
    assert (corrected.holder, corrected.review_at, corrected.strike_count) == (
        held.holder, held.review_at, held.strike_count)


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


def test_say_without_answers_answers_the_target_units_open_asks(held_commander, capsys):
    from test_holder_standing_rules import publish

    ctx = held_commander
    comms = ctx.comms
    reply = ctx.root / "answer.txt"
    reply.write_text("Take the owner fix at the declaration and push it.")
    named = publish(comms, "worker", ctx.commander, time.time() - 960)
    capsys.readouterr()
    main(["say", "worker", str(reply), "--answers", named.message_id], ctx)
    assert "answers:" not in capsys.readouterr().out  # an explicit list overrides
    unnamed = publish(comms, "worker", ctx.commander, time.time() - 930)
    main(["say", "worker", str(reply)], ctx)
    assert f"answers: {unnamed.message_id}" in capsys.readouterr().out
    history = ThreadCaller(ctx.commander).invoke(CommsHistoryTool(peer="worker"), comms)
    assert [message.answers for message in history.messages
            if message.sender == ctx.commander][:2] == [(unnamed.reference,), (named.reference,)]
