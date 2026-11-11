"""A holder claims through the real tool boundary on a copied bus."""
import json
import os
import shlex
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import chdir, contextmanager
from dataclasses import replace

import pytest

from agent_comms.coordination.errors import (
    CoordinationBeginUnavailable,
    CoordinationReadUnavailable,
)
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.errors import (
    HumanSendRefusedError,
    TodoError,
)
from agent_comms.messaging.bus_tables import AuthoredAnswers
from agent_comms.messaging.messages import HandoffKind, Message, QuestionKind
from agent_comms.nominal.field_codec import (
    FieldCodec,
    GoalId,
    OrderId,
    RecordProblems,
    ToolCallId,
)
from agent_comms.planning.goal_actions import SetGoalAction
from agent_comms.planning.goals import (
    OrderCheck,
    ProblemGoalOrder,
)
from agent_comms.planning.order_runs import ApplicationOrderRun
from agent_comms.planning.order_words import (
    DeliveryReport,
    StrikeReason,
)
from agent_comms.planning.orders import (
    CheckedProblemOrder,
    ClaimedProblemOrder,
    ClaimOrder,
    ClaimPublicationUnavailable,
    CorrectCheckTodo,
    DeliveredProblemOrder,
    DoneTodoState,
    FailedCheckStrike,
    OrderClaim,
    OrderMessage,
    PendingProblemOrder,
    ReleaseTodo,
    ResumeClaimOrder,
    SelectedClaimReviewer,
    SendBackVerdict,
    StrikeTodo,
    Todo,
    TodoConflict,
    TriageScope,
)
from agent_comms.planning.problem_findings import ExpectedProblem
from agent_comms.planning.pull_requests import PullRequestLink
from agent_comms.projects.change_report import FileLine
from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.slop import (
    DeclarationDerivation,
    DeleteCopyDerivation,
    Derivation,
    ExistingLine,
    Slop,
)
from agent_comms.registry.thread_status import PausedBridgeThreadStatus
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.locked_store import SQLiteFile
from agent_comms.storage.recorded_time import RecordedEventTime
from agent_comms.storage.store_files import StoreBusy, StoreWait
from agent_comms.team.order import OrderFile, TeamContext, main
from agent_comms.team.standing import DeliveryAwaitingJudgement, Scrutinize
from agent_comms.team.standing_rules import OwedAnswer
from agent_comms.transport.tool_caller import ThreadCaller
from agent_comms.transport.tools.orders import (
    CommsOrderDeliverTool,
    CommsVerdictTool,
    TodoChanged,
)
from agent_comms.transport.tools.served import CommsHistoryTool
from agent_comms_hooks import ToolRefusedError
from test_held_problem_orders import merge_two_commit_fix
from test_todos import order, thread

pytest_plugins = ("test_held_problem_orders",)


def working(ctx) -> Todo:
    """The holder works P1 under its own problem goal."""
    original = ctx.comms.goals.todos.get("P1")
    ctx.comms.goals.update_goal("worker", SetGoalAction(
        text=ProblemGoalOrder.text, order=ProblemGoalOrder(),
        goal_id=original.held().generation,
    ))
    return original


def delivery(ctx, report: str) -> CommsOrderDeliverTool:
    """The holder's claim of P1 at the checkout's HEAD through pull request 1."""
    return CommsOrderDeliverTool(
        problem="P1", pull_request=PullRequestLink(ctx.comms.goals.todos.get("P1").repo, 1),
        head=ctx.repos[0].git(ctx.repos[0].checkout, "rev-parse", "HEAD").strip(),
        report=DeliveryReport(report))


def test_pathname_handoff_publishes_without_completing_the_held_work(held_team):
    comms = held_team.comms
    colleague = held_team.actor_name
    original = comms.goals.todos.get(working(held_team).id)
    goals = tuple(comms.registry.require(name).goal for name in ("worker", colleague))
    body = (
        "Recording saved at `/home/ts/.cache/agent-scratch/probe/toad-comms-fixed`; "
        "investigation continues."
    )

    committed = comms.messaging.send("worker", colleague, body, HandoffKind).message
    stored = comms.bus.bus.lookup_message(committed.message_id).message

    assert stored.body == body
    assert (stored.sender, stored.target, stored.type) == ("worker", colleague, HandoffKind)
    assert Message.from_message(stored).to_wire() == Message.from_message(committed).to_wire()
    assert stored.type.allows_automatic_reply(stored)
    assert comms.bus.bus.unanswered_references(
        (stored.seq,), AuthoredAnswers(frozenset({colleague})),
    ) == (stored.reference,)
    assert tuple(comms.registry.require(name).goal for name in ("worker", colleague)) == goals
    assert comms.goals.todos.get(original.id) == original


def test_delivery_report_refuses_a_flat_status_memo():
    with pytest.raises(ToolRefusedError, match="name the slop you killed"):
        DeliveryReport("Ready at abc; check 13 -> 0; tests pass")
    blunt = "Killed the string slop at turn_watchdog.py:137; the typed state answers it now."
    assert DeliveryReport(blunt).text == blunt


def test_a_delivery_records_the_repository_its_pull_request_lives_in(held_team):
    """An order in one repo can be delivered by a pull request in another (Core order, Textual PR)."""
    from agent_comms.projects.repository_name import RepositoryName

    ctx = held_team
    comms = ctx.comms
    original = working(ctx)
    sha = ctx.repos[0].git(ctx.repos[0].checkout, "rev-parse", "HEAD").strip()
    request = FieldCodec.decode(CommsOrderDeliverTool, {
        "tool": "comms_order_deliver", "problem": "P1",
        "pull_request": {"repo": "openhcsdev/textual", "number": 213}, "head": sha,
        "report": "Killed the broken Core#213 lie at order_tools.py:398; the claim names Textual#213 now.",
    })
    ThreadCaller("worker").invoke(request, comms)
    claim = comms.goals.todos.get("P1").order.claim
    assert claim.pull_request == PullRequestLink(RepositoryName.from_wire("openhcsdev/textual"), 213)
    assert claim.pull_request.repo != original.repo


def current_parent_order(ctx: TeamContext) -> Todo:
    """A current assignment under a request opened by the previous teammate."""
    comms = ctx.comms
    original = comms.goals.todos.get("P1")
    parent = thread("current-parent", ctx.root.parent / "current-parent", 4.0)
    holder = thread("claim-worker", ctx.root.parent / "claim-worker", 5.0)
    comms.registry.register(parent)
    comms.registry.register(holder)
    todo = comms.goals.todos.create(
        "P2", original.repo, "Repair the workflow under its current teammate",
        creator=comms.registry.require(ctx.actor_name), assigner=parent.incarnation,
        owner=holder, generation=GoalId.mint(),
        request=original.request.require(), review_at=RecordedEventTime(time.time() + 1000),
        order=replace(original.order.checked(), id=OrderId.mint()),
    )
    sent = comms.messaging.send(
        parent.name, holder.name, "Repair the workflow and report to the current teammate.",
    ).message
    return comms.goals.todos.apply(todo.id, OrderMessage(
        todo.revision, holder.incarnation, sent.message_id, at=RecordedEventTime(time.time()),
    ))


@pytest.mark.parametrize("historical_state", ("retired", "reborn"))
def test_claim_uses_current_parent_not_retired_request_author(held_team, historical_state):
    ctx = held_team
    comms = ctx.comms
    original = current_parent_order(ctx)
    historical = ctx.actor_name
    comms.registry.unregister(historical)
    comms.registry.remove(historical)
    if historical_state == "reborn":
        comms.registry.register(thread(historical, ctx.root.parent / "reborn-author", 8.0))
    request = replace(delivery(
        ctx, "Killed the historical-author routing slop at todos.py; the assignment owns the target."),
        problem=original.id,
    )
    ThreadCaller(original.holder).invoke(request, comms)
    claimed = comms.goals.todos.get(original.id)
    claim = claimed.order.claim
    published = comms.bus.bus.lookup_message(claim.message.message_id).message
    assert published.target == original.held().parent_identity.name == "current-parent"
    assert published.answers == claim.message.answers
    before = comms.bus.bus.revision()
    ThreadCaller(original.holder).invoke(request, comms)
    assert comms.goals.todos.get(original.id) == claimed
    assert comms.bus.bus.revision() == before


@pytest.mark.parametrize("parent_state", ("retired", "reborn"))
def test_claim_refuses_retired_or_reborn_current_parent(held_team, parent_state):
    ctx = held_team
    comms = ctx.comms
    original = current_parent_order(ctx)
    parent = original.held().parent_identity.name
    comms.registry.unregister(parent)
    comms.registry.remove(parent)
    if parent_state == "reborn":
        comms.registry.register(thread(parent, ctx.root.parent / "reborn-parent", 9.0))
    before = comms.bus.bus.revision()
    request = replace(delivery(
        ctx, "Killed the historical-author routing slop at todos.py; the assignment owns the target."),
        problem=original.id,
    )
    with pytest.raises(TodoError, match=f"@{parent} will not answer this review"):
        ThreadCaller(original.holder).invoke(request, comms)
    assert comms.goals.todos.get(original.id) == original
    assert comms.bus.bus.revision() == before


def self_assigned_order(ctx: TeamContext) -> Todo:
    """The holder accepts its existing work as itself, not as an invented peer."""
    todo = current_parent_order(ctx)
    holder = ctx.comms.registry.require(todo.holder)
    request = ctx.comms.goals.todos.add_request(
        holder.incarnation, "Deliver the self-assigned repair to an independent reviewer",
    ).id
    assigned, = ctx.comms.goals.todos.assign(
        (ExpectedProblem(todo.id, todo.revision),), owner=holder, assigner=holder.incarnation,
        generation=todo.held().generation, request=request,
        review_at=todo.review_at, order=todo.order.checked(),
    )
    assert assigned.held().parent_identity == holder.incarnation
    return assigned


def test_self_assigned_claim_selects_a_reviewer_and_retains_its_choice(held_team):
    ctx = held_team
    comms = ctx.comms
    todo = self_assigned_order(ctx)
    request = replace(delivery(
        ctx, "Killed the self-send slop at todos.py; the selected reviewer owns the notification."),
        problem=todo.id,
    )
    before = comms.bus.bus.revision()
    with pytest.raises(TodoError, match="name its reviewer"):
        ThreadCaller(todo.holder).invoke(request, comms)
    assert comms.goals.todos.get(todo.id) == todo
    assert comms.bus.bus.revision() == before
    reviewer = SelectedClaimReviewer(comms.registry.require(ctx.actor_name).incarnation)
    request = replace(request, reviewer=reviewer)
    # The same declared request shape reaches the native boundary and the retained record.
    request = FieldCodec.decode(CommsOrderDeliverTool, FieldCodec.encode(request))
    ThreadCaller(todo.holder).invoke(request, comms)
    claimed = comms.goals.todos.get(todo.id)
    claim = claimed.order.claim
    assert claim.reviewer == reviewer
    assert FieldCodec.decode(Todo, FieldCodec.encode(claimed)) == claimed
    published = comms.bus.bus.lookup_message(claim.message.message_id).message
    assert published.target == ctx.actor_name
    after = comms.bus.bus.revision()
    ThreadCaller(todo.holder).invoke(request, comms)
    assert comms.bus.bus.revision() == after
    assert comms.goals.todos.get(todo.id) == claimed
    different = replace(request, reviewer=SelectedClaimReviewer(
        comms.registry.require("current-parent").incarnation))
    with pytest.raises(TodoError, match="Only a pending order"):
        ThreadCaller(todo.holder).invoke(different, comms)
    assert comms.goals.todos.get(todo.id) == claimed
    assert comms.bus.bus.revision() == after


def test_a_claim_follows_its_pull_request_to_a_new_head(held_team):
    """A push after delivery moves the claim, so the verdict reads the head that ships."""
    ctx = held_team
    comms = ctx.comms
    todo = self_assigned_order(ctx)
    reviewer = SelectedClaimReviewer(comms.registry.require(ctx.actor_name).incarnation)
    request = replace(delivery(
        ctx, "Killed the frozen-claim slop at orders.py; the verdict reads the shipping head."),
        problem=todo.id, reviewer=reviewer)
    ThreadCaller(todo.holder).invoke(request, comms)
    repo = ctx.repos[0]
    repo.git(repo.checkout, "commit", "--allow-empty", "-q", "-m", "Pushed again")
    moved = replace(request, head=repo.git(repo.checkout, "rev-parse", "HEAD").strip())
    ThreadCaller(todo.holder).invoke(moved, comms)
    claim = comms.goals.todos.get(todo.id).order.claim
    assert claim.head == moved.head
    assert comms.bus.bus.lookup_message(claim.message.message_id).message.target == ctx.actor_name
    elsewhere = replace(moved, head=request.head,
                        pull_request=PullRequestLink(moved.pull_request.repo, 2))
    with pytest.raises(TodoError, match="new head of its own pull request"):
        ThreadCaller(todo.holder).invoke(elsewhere, comms)
    assert comms.goals.todos.get(todo.id).order.claim == claim


def away_reviewer(ctx: TeamContext) -> NativeThread:
    return NativeThread("away-reviewer", frozenset(),
                        Checkout(ctx.root.parent / "away-reviewer"), created_at=16.0)


def away_reviewer_delivery(ctx: TeamContext, teammate: NativeThread):
    """A self-assigned claim naming a reviewer whose bridge paused."""
    todo = self_assigned_order(ctx)
    ctx.comms.registry.register(teammate, PausedBridgeThreadStatus())
    reviewer = SelectedClaimReviewer(ctx.comms.registry.require("away-reviewer").incarnation)
    return todo, replace(delivery(
        ctx, "Killed the unjudged-claim slop at orders.py; a reviewer that reads gets it."),
        problem=todo.id, reviewer=reviewer)


def test_a_claim_to_a_reviewer_that_will_never_read_it_is_refused(held_team):
    """On 2026-11-10 mate-a's P1444 claim sat for a stopped claude-a: nothing read it."""
    comms = held_team.comms
    todo, request = away_reviewer_delivery(held_team, away_reviewer(held_team))
    before = comms.bus.bus.revision()
    with pytest.raises(TodoError, match="@away-reviewer will not answer this review"):
        ThreadCaller(todo.holder).invoke(request, comms)
    assert comms.goals.todos.get(todo.id) == todo
    assert comms.bus.bus.revision() == before


def test_a_claim_to_a_paused_reviewer_whose_process_lives_is_recorded(held_team):
    comms = held_team.comms
    todo, request = away_reviewer_delivery(held_team, replace(
        away_reviewer(held_team), process_identity=ProcessIdentity.capture(os.getpid())))
    ThreadCaller(todo.holder).invoke(request, comms)
    claim = comms.goals.todos.get(todo.id).order.claim
    assert claim.reviewer == request.reviewer
    assert comms.bus.bus.lookup_message(claim.message.message_id).message.target == "away-reviewer"


@pytest.mark.parametrize("change", ("retired", "reborn", "renamed"))
def test_selected_reviewer_resume_preserves_original_identity_and_address(held_team, change):
    ctx = held_team
    comms = ctx.comms
    todo = self_assigned_order(ctx)
    name = "current-parent"
    reviewer = SelectedClaimReviewer(comms.registry.require(name).incarnation)
    request = replace(delivery(
        ctx, "Killed the self-send slop at todos.py; the selected reviewer owns the notification."),
        problem=todo.id, reviewer=reviewer,
    )
    ThreadCaller(todo.holder).invoke(request, comms)
    claimed = comms.goals.todos.get(todo.id)
    if change == "renamed":
        comms.registry.rename(name, "renamed-reviewer")
    else:
        comms.registry.unregister(name)
        comms.registry.remove(name)
        if change == "reborn":
            comms.registry.register(thread(name, ctx.root.parent / "reborn-reviewer", 15.0))
    before = comms.bus.bus.revision()
    with pytest.raises((TodoError, HumanSendRefusedError)):
        ThreadCaller(todo.holder).invoke(request, comms)
    assert comms.goals.todos.get(todo.id) == claimed
    assert comms.bus.bus.revision() == before


def test_claim_verdict_family_on_retained_copy(held_team):
    ctx = held_team
    comms = ctx.comms
    original = working(ctx)
    report = ctx.root / "report.txt"
    report.write_text("Killed the broken count copy; inspect its actual diff before accepting it.")
    request = delivery(ctx, report.read_text())
    with pytest.raises(TodoConflict):
        ThreadCaller(ctx.actor_name).invoke(request, comms)
    assert comms.goals.todos.get("P1") == original

    ThreadCaller("worker").invoke(request, comms)
    claimed = comms.goals.todos.get("P1")
    assert isinstance(claimed.order, ClaimedProblemOrder)
    assert not claimed.pending
    assert FieldCodec.decode(Todo, FieldCodec.encode(claimed)) == claimed
    assert claimed.order.claim.head == request.head
    assert claimed.order.claim.message.body == report.read_text()
    history = ThreadCaller(ctx.actor_name).invoke(CommsHistoryTool(peer="worker"), comms)
    sent = next(message for message in history.messages
                if message.message_id == claimed.order.claim.message.message_id)
    assert sent.sender == "worker" and sent.target == ctx.actor_name
    assert sent.answers and sent.answers == claimed.order.claim.message.answers
    # The identical call resumes the retained claim's publication: the same Todo, one original.
    ThreadCaller("worker").invoke(request, comms)
    assert comms.goals.todos.get("P1") == claimed
    history = ThreadCaller(ctx.actor_name).invoke(CommsHistoryTool(peer="worker"), comms)
    assert [message.message_id for message in history.messages].count(
        claimed.order.claim.message.message_id) == 1
    other = replace(request, report=DeliveryReport(
        report.read_text() + " The tests it changed need reading too."))
    with pytest.raises(TodoError, match="Only a pending order"):
        ThreadCaller("worker").invoke(other, comms)
    assert comms.goals.todos.get("P1") == claimed

    why = StrikeReason(
        "This is stupid because the claimed change leaves the original duplicated decision in place."
    )
    ThreadCaller(ctx.actor_name).invoke(CommsVerdictTool(
        problem="P1", operation=SendBackVerdict(
            claimed.revision, ctx.incarnation, claimed.held(), ToolCallId("fixture-call"), why,
            evidence=(claimed.order.owner,),
        ),
    ), comms)
    pending = comms.goals.todos.get("P1")
    assert isinstance(pending.order, PendingProblemOrder)
    assert pending.order.id != claimed.order.id
    assert why.text in pending.order.edits
    assert claimed.order in pending.history.events(pending.id, CheckedProblemOrder)
    assert pending.strike_count == claimed.strike_count + 1
    response = CommsOrderDeliverTool.invoke(comms, request.arguments, caller=ThreadCaller("worker"))
    assert isinstance(FieldCodec.decode(TodoChanged, response).todo.order, ClaimedProblemOrder)
    with chdir(ctx.repos[0].checkout):
        main(["done", "worker", "P1"], ctx)
    delivered = comms.goals.todos.get("P1")
    assert isinstance(delivered.order, DeliveredProblemOrder)
    assert delivered.order.delivery.delivered == delivered.order.delivery.commit
    assert not delivered.pending


def test_a_busy_begin_answers_every_storage_error_handler() -> None:
    """A store's handlers name sqlite3.Error; the bridge's waits name the coordination class."""
    raw = sqlite3.OperationalError("database is locked")
    raw.sqlite_errorcode, raw.sqlite_errorname = sqlite3.SQLITE_BUSY, "SQLITE_BUSY"
    with pytest.raises(sqlite3.Error) as caught, CoordinationBeginUnavailable.checking():
        raise raw
    assert isinstance(caught.value, CoordinationBeginUnavailable)
    assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
    # An enclosing read decoder leaves it as it is: its handlers name this exact class.
    with pytest.raises(CoordinationBeginUnavailable), CoordinationReadUnavailable.checking():
        raise caught.value


@contextmanager
def held_bus_writer(ctx: TeamContext):
    """Hold the real writer; the blocked publication need not spend an outage budget."""
    with SQLiteFile.writers_drained(ctx.comms.bus.bus.path), pytest.MonkeyPatch.context() as patch:
        patch.setattr(StoreWait, "timeout", StoreBusy.timeout)
        yield


def unsent_claim(ctx):
    """A real claim whose Todo commits while another writer holds the bus."""
    comms = ctx.comms
    working(ctx)
    request = delivery(
        ctx, "Killed the stupid duplicated count decision; read its actual diff before accepting it.")
    before = comms.bus.bus.revision()
    # SQLite refuses the real locked writer; publication and claim logic stay unpatched.
    with held_bus_writer(ctx):
        with pytest.raises(ClaimPublicationUnavailable) as unsent:
            ThreadCaller("worker").invoke(request, comms)
    claimed = comms.goals.todos.get("P1")
    assert isinstance(claimed.order, ClaimedProblemOrder)
    assert unsent.value.claim == claimed.order.claim
    assert comms.bus.bus.revision() == before
    return request, claimed, before


def test_an_unsent_claim_stays_owed_and_the_same_call_publishes_it_once(held_team):
    ctx = held_team
    comms = ctx.comms
    request, claimed, before = unsent_claim(ctx)
    claim = claimed.order.claim
    # Every owed-answer gate reads the retained claim; no bus record is required.
    opened = OwedAnswer.opened(comms, ctx.incarnation)
    owed, = (rule for rule in opened if isinstance(rule, DeliveryAwaitingJudgement))
    assert (owed.problem, owed.request, owed.message) == ("P1", claim.request(), claim.message)
    assert owed.cited == f"P1 claim of {claim.head[:9]}"
    assert not owed.answered_by((claim.message,))
    # Two identical resumptions at once publish one original with its retained content.
    with ThreadPoolExecutor(max_workers=2) as pool:
        for resumed in [pool.submit(ThreadCaller("worker").invoke, request, comms)
                        for _ in range(2)]:
            resumed.result(timeout=60)
    assert comms.bus.bus.revision() == before + 1
    published = comms.bus.bus.lookup_message(claim.message.message_id).message
    assert published.publication_snapshot == claim.message.publication_snapshot
    assert published.timestamp == claim.message.timestamp
    assert comms.goals.todos.get("P1") == claimed
    # A repeat after success is the same original, never a second attention delivery.
    ThreadCaller("worker").invoke(request, comms)
    assert comms.bus.bus.revision() == before + 1
    assert comms.goals.todos.get("P1") == claimed
    owed, = (rule for rule in OwedAnswer.opened(comms, ctx.incarnation)
             if isinstance(rule, DeliveryAwaitingJudgement))
    assert owed.request == claim.request()


def test_a_resume_publishes_only_the_order_s_current_claim(held_team):
    """A claim the order never held, or a resume prepared before a real send-back replaced the
    claim, refuses in the Todo transaction: no bus original and no Todo change."""
    ctx = held_team
    comms = ctx.comms
    _request, claimed, before = unsent_claim(ctx)
    worker = comms.registry.require("worker").incarnation
    generation = claimed.held().generation
    claim = claimed.order.claim
    never_retained = replace(claim, head="b" * 40)
    with pytest.raises(TodoError, match="does not hold the claim"):
        ResumeClaimOrder(claimed.revision, worker, generation, never_retained).record(
            comms.goals, "P1")
    assert comms.bus.bus.revision() == before
    assert comms.goals.todos.get("P1") == claimed
    prepared = ResumeClaimOrder(claimed.revision, worker, generation, claim)
    why = StrikeReason(
        "This is stupid because the claimed change leaves the original duplicated decision in place."
    )
    ThreadCaller(ctx.actor_name).invoke(CommsVerdictTool(
        problem="P1", operation=SendBackVerdict(
            claimed.revision, ctx.incarnation, claimed.held(), ToolCallId("fixture-call"), why,
            evidence=(claimed.order.owner,),
        ),
    ), comms)
    replaced = comms.goals.todos.get("P1")
    assert claim not in replaced.order.team_claims()
    sent = comms.bus.bus.revision()
    with pytest.raises(TodoError):
        prepared.record(comms.goals, "P1")
    assert comms.bus.bus.revision() == sent
    assert comms.goals.todos.get("P1") == replaced


def test_a_published_claim_retried_on_a_held_bus_is_unavailable_never_unsent(held_team):
    """A retry that cannot take the bus writer cannot see the original it already published:
    it reports the publication unavailable, and after release returns that original."""
    ctx = held_team
    comms = ctx.comms
    working(ctx)
    request = delivery(
        ctx, "Killed the stupid duplicated count decision; read its actual diff before accepting it.")
    ThreadCaller("worker").invoke(request, comms)
    claimed = comms.goals.todos.get("P1")
    published = comms.bus.bus.revision()
    with held_bus_writer(ctx):
        with pytest.raises(ClaimPublicationUnavailable) as unavailable:
            ThreadCaller("worker").invoke(request, comms)
    assert isinstance(unavailable.value.__cause__, sqlite3.Error)
    assert "could not be completed" in str(unavailable.value)
    assert comms.bus.bus.revision() == published
    ThreadCaller("worker").invoke(request, comms)
    assert comms.bus.bus.revision() == published
    assert comms.bus.bus.lookup_message(claimed.order.claim.message.message_id).seq == published
    assert comms.goals.todos.get("P1") == claimed


def test_an_unsent_claim_is_never_resent_under_a_renamed_team(held_team):
    ctx = held_team
    comms = ctx.comms
    request, claimed, before = unsent_claim(ctx)
    comms.registry.rename(ctx.actor_name, "renamed-colleague")
    with pytest.raises(TodoError, match="retained claim was addressed"):
        ThreadCaller("worker").invoke(request, comms)
    assert comms.bus.bus.revision() == before
    assert comms.goals.todos.get("P1") == claimed


def test_claimed_check_correction_reads_the_base_the_fix_was_built_on(
    held_team, cited_source, tmp_path,
):
    """Once the claimed PR merges, current main passes every correct check; the claim's own
    branch point still holds the defect, so a correct check is accepted and done records it."""
    ctx = held_team
    comms = ctx.comms
    base, first, merge = merge_two_commit_fix(cited_source, tmp_path / "bin", 4)
    working(ctx)
    head = cited_source.git(cited_source.checkout, "rev-parse", "fix/4").strip()
    ThreadCaller("worker").invoke(CommsOrderDeliverTool(
        problem="P1", pull_request=PullRequestLink(comms.goals.todos.get("P1").repo, 4), head=head,
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


def test_failed_check_strike_keeps_the_holder_at_the_updated_revision(held_team):
    ctx = held_team
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

    struck = store.apply(first.id, failure)
    assert struck.assignment == first.assignment
    assert struck.order == first.order
    assert struck.revision == first.revision + 1
    assert struck.strike_count == first.strike_count + 1
    latest_failure, = struck.history.events(struck.id, FailedCheckStrike, limit=1, descending=True)
    assert latest_failure == failure
    assert not struck.history.events(struck.id, ReleaseTodo)
    with pytest.raises(TodoConflict):
        store.apply(first.id, failure)
    assert store.get(first.id) == struck


def test_third_strike_keeps_holder_and_order_and_requests_scrutiny(held_team):
    ctx = held_team
    store = ctx.comms.goals.todos
    original = todo = store.get("P1")
    for ordinal in range(3):
        todo = store.apply(todo.id, StrikeTodo(
            todo.revision, ctx.incarnation, todo.held(),
            f"Review {ordinal + 1} found the original duplicated decision still present in the edited source.",
            evidence=(todo.order.checked().owner,),
        ))
        assert todo.held() == original.held()
        assert todo.order == original.order
    assert todo.strikes_against(ctx.comms.registry.require("worker").incarnation) == 3
    assert not todo.history.events(todo.id, ReleaseTodo)
    scrutiny, = Scrutinize.violations(ctx.comms)
    assert scrutiny.holder == todo.held()
    assert scrutiny.todo == todo
    assert scrutiny.count == 3
    main(["unstrike", "P1", "--why",
          "The third strike counted the same duplicated decision from the earlier review twice."], ctx)
    after = store.get("P1")
    assert after.held() == original.held()
    assert after.order == original.order
    assert after.strike_count == 2


def test_unstrike_does_not_undo_an_explicit_release(held_team):
    ctx = held_team
    store = ctx.comms.goals.todos
    todo = store.get('P1')
    struck = store.apply(todo.id, StrikeTodo(
        todo.revision, ctx.incarnation, todo.held(),
        'The first review counted a duplicated decision that the source did not contain.',
        evidence=(todo.order.checked().owner,)))
    released = store.apply(todo.id, ReleaseTodo(
        struck.revision, struck.held().owner_identity, struck.held(),
        StrikeReason('The holder deliberately releases this exact assignment for independent work.')))
    releases = released.history.events(released.id, ReleaseTodo)
    main(['unstrike', 'P1', '--why',
          'The cited duplicated decision was absent and this strike was recorded in error.'], ctx)
    after = store.get(todo.id)
    assert after.holder is None
    assert not after.live_strikes()
    assert after.history.events(after.id, ReleaseTodo) == releases


def claimed_by_worker(ctx) -> Todo:
    working(ctx)
    ThreadCaller("worker").invoke(delivery(
        ctx, "Killed the broken count copy; inspect its actual diff before accepting it."), ctx.comms)
    claimed = ctx.comms.goals.todos.get("P1")
    assert isinstance(claimed.order, ClaimedProblemOrder)
    return claimed


def corrected_order_file(ctx, source) -> str:
    marker = ctx.root / "duplicated-decision"
    marker.touch()
    now = time.time()
    document = OrderFile(
        replace(order(source), id=OrderId.mint(),
                check=OrderCheck(ctx.repos[0].checkout, f"test ! -e {shlex.quote(str(marker))}"),
                at=RecordedEventTime(now), first_call_deadline=RecordedEventTime(now + 120)),
        "The claimed change keeps the duplicated decision the order told the teammate to delete",
        "This is stupid because the teammate's order named the wrong member to delete",
        unplanned_reason="This isolated workflow verifies claimed order correction rather than advancing a plan.",
        problems=("P1",),
    )
    path = ctx.root / "corrected-order.json"
    path.write_text(json.dumps(FieldCodec.encode(document)))
    return str(path)


def test_corrected_order_to_the_claimant_replaces_its_claim(held_team, cited_source):
    ctx = held_team
    claimed = claimed_by_worker(ctx)
    main(["send", "worker", corrected_order_file(ctx, cited_source), "--clean", "--as", "worker"], ctx)
    corrected = ctx.comms.goals.todos.get("P1")
    assert isinstance(corrected.order, PendingProblemOrder)
    assert corrected.order.id != claimed.order.id
    assert claimed.order in corrected.history.events(corrected.id, CheckedProblemOrder)
    assert corrected.holder == "worker"
    assert corrected.strike_count == claimed.strike_count


def test_two_delivery_claims_warn_without_deadlocking_other_orders(
    held_team, cited_source, capsys,
):
    other_order_goal_id = GoalId.mint()
    ctx = held_team
    comms = ctx.comms
    store = comms.goals.todos
    source = ctx.repos[0]
    checkout = ctx.root.parent / "other-claim-checkout"
    source.git(source.checkout, "worktree", "add", "-q", "--detach", str(checkout))
    other = thread("other", checkout, 3.0)
    comms.registry.register(other)
    comms.registry.register(thread("third", ctx.root.parent / "third", 4.0))
    colleague = comms.registry.require(ctx.actor_name)
    original = store.get("P1")
    second = store.create(
        "P2", original.repo, "Repair the other held workflow", creator=colleague,
        assigner=ctx.incarnation, owner=other, generation=other_order_goal_id,
        request=original.request.require(), review_at=RecordedEventTime(time.time() + 1000),
        order=replace(original.order.checked(), id=OrderId.mint()),
    )
    head = source.git(source.checkout, "rev-parse", "HEAD").strip()
    claims = []
    for todo in (original, second):
        holder = comms.registry.require(todo.holder)
        sent = comms.messaging.send(
            ctx.actor_name, holder.name, todo.order.instructions(todo),
        ).message
        claim = OrderClaim.for_order(
            todo, holder.incarnation, PullRequestLink(todo.repo, 1), head, "The source repair is published for review.",
            (sent.reference,),
        )
        claim = replace(claim, message=replace(claim.message, timestamp=time.time() - 3600))
        claims.append(ClaimOrder(
            todo.revision, holder.incarnation, todo.held().generation, claim,
        ).record(comms.goals, todo.id))

    reply = ctx.root / "claim-status.txt"
    reply.write_text("Waiting on the review, owner colleague.")
    owed = {rule.message.message_id: rule for rule in OwedAnswer.opened(comms, ctx.incarnation)}
    capsys.readouterr()
    main(["say", "third", str(reply)], ctx)
    warning = capsys.readouterr().out
    for claimed in claims:
        assert f"WARNING: {owed[claimed.order.claim.message.message_id].line()}" in warning
    assert store.get("P1") == claims[0]
    assert store.get("P2") == claims[1]
    with pytest.raises(RecordProblems) as refused:
        main(["say", "worker", str(reply), "--answers",
              claims[0].order.claim.message.message_id], ctx)
    assert owed[claims[0].order.claim.message.message_id].cited in refused.value.problems
    assert store.get("P1") == claims[0]
    ask = comms.messaging.send(
        "worker", ctx.actor_name, "Which owner do I wait on?", type=QuestionKind,
    ).message
    main(["say", "worker", str(reply), "--answers", ask.message_id], ctx)
    assert store.get("P1") == claims[0]

    main(["send", "worker", corrected_order_file(ctx, cited_source), "--clean", "--as", "worker"], ctx)
    corrected = store.get("P1")
    assert isinstance(corrected.order, PendingProblemOrder)
    assert corrected.order.team_claims() == ()
    assert claims[0].order in corrected.history.events(corrected.id, CheckedProblemOrder)
    assert store.get("P2") == claims[1]


def test_claimant_can_hand_off_unfinished_work_without_losing_its_claim(held_team, cited_source):
    ctx = held_team
    ctx.comms.registry.register(thread("other", ctx.root, 3.0))
    claimed = claimed_by_worker(ctx)
    main(["send", "other", corrected_order_file(ctx, cited_source), "--clean", "--reassign",
          "the claimant transfers the unfinished work while preserving its recorded claim",
          "--as", "worker"], ctx)
    following = ctx.comms.goals.todos.get("P1")
    assert following.held().owner_identity == ctx.comms.registry.require("other").incarnation
    assert isinstance(following.order, PendingProblemOrder)
    assert claimed.order in following.history.events(following.id, CheckedProblemOrder)


def test_claimed_order_takes_a_check_correction_through_the_real_verb(held_team):
    ctx = held_team
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


def test_held_application_order_takes_a_run_correction_through_the_real_verb(held_team):
    ctx = held_team
    store = ctx.comms.goals.todos
    current = store.get("P1")
    stale = ApplicationOrderRun("The turn view shows the teammate's newest turn after it ends.")
    with store.writing() as connection:
        replace(current, order=replace(current.order, run=stale),
                revision=current.revision + 1).upsert(connection)
    held = store.get("P1")

    main(["run", "P1", "--run", "The order story shows the teammate's newest tool call after it ends.",
          "--why", "the recorded run names a turn view that a merged deletion removed"], ctx)
    corrected = store.get("P1")
    assert corrected.order.run == ApplicationOrderRun(
        "The order story shows the teammate's newest tool call after it ends.")
    assert corrected.order.corrections[-1].previous == stale
    assert (corrected.holder, corrected.review_at, corrected.strike_count) == (
        held.holder, held.review_at, held.strike_count)


def test_closed_problem_refuses_a_check_correction_and_writes_nothing(held_team):
    ctx = held_team
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


def test_say_without_answers_answers_the_targets_open_asks(held_team, capsys):
    from test_holder_standing_rules import publish

    ctx = held_team
    comms = ctx.comms
    reply = ctx.root / "answer.txt"
    reply.write_text("Take the owner fix at the declaration and push it.")
    named = publish(comms, "worker", ctx.actor_name, time.time() - 960)
    capsys.readouterr()
    main(["say", "worker", str(reply), "--answers", named.message_id], ctx)
    assert "answers:" not in capsys.readouterr().out  # an explicit list overrides
    unnamed = publish(comms, "worker", ctx.actor_name, time.time() - 930)
    main(["say", "worker", str(reply)], ctx)
    assert f"answers: {unnamed.message_id}" in capsys.readouterr().out
    history = ThreadCaller(ctx.actor_name).invoke(CommsHistoryTool(peer="worker"), comms)
    assert [message.answers for message in history.messages
            if message.sender == ctx.actor_name][:2] == [(unnamed.reference,), (named.reference,)]


def test_a_measuring_order_owes_no_restated_fact(held_team, cited_source):
    """2026-10-30: Tristan's Toad profiling request sat with no order because the recurrence gate
    asked a measurement for the fact it was being sent to find."""
    store = held_team.comms.goals.todos
    fix = replace(store.get("P1").order.checked(), id=OrderId.mint())
    where = fix.owner
    with pytest.raises(TodoError):
        store.require_recurrence_declaration(fix, where, cited_source.checkout, (), 20)
    store.require_recurrence_declaration(
        replace(fix, scope=TriageScope()), where, cited_source.checkout, (), 20)


def test_recurrence_refusal_names_accepted_derivations(held_team, cited_source):
    ctx = held_team
    store = ctx.comms.goals.todos
    original = store.get("P1")
    planned = replace(original.order.checked(), id=OrderId.mint())
    where = planned.owner
    outside = FileLine("owner.py", 1)
    rejected = (
        Slop("IMPL-3", "the count of open orders", where,
             DeleteCopyDerivation, ExistingLine(outside)),
        Slop("IMPL-3", "the count of open orders", where,
             DeclarationDerivation, ExistingLine(where)),
    )
    with pytest.raises(TodoError) as refusal:
        store.require_recurrence_declaration(planned, where, cited_source.checkout, rejected, 20)
    text = str(refusal.value)
    for mechanism in Derivation.members_with(DeclarationDerivation):
        assert mechanism.declared_name in text
    assert "derive delete-copy never satisfies recurrence" in text
    assert f"by {where.to_wire()} is inside the same declaration" in text
    assert "Parent" in text and "owner.py:2-3" in text
    assert store.get(original.id) == original

    accepted = replace(rejected[0], derive=DeclarationDerivation)
    store.require_recurrence_declaration(planned, where, cited_source.checkout, (accepted,), 20)
    assert store.get(original.id) == original

    document = OrderFile(planned,
        "The slop repeats the count instead of reading its declaration",
        "This is stupid because the same count has two independent decisions")
    guidance = "\n".join(document.problems_found(clean=True))
    for mechanism in Derivation.members_with(Derivation):
        assert mechanism.declared_name in guidance
    assert "src/agent_comms/FILE.py:LINE" in guidance
    json.loads(guidance.split("this JSON schema: ", 1)[1])
