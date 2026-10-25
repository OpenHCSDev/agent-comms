"""Board review commands and judge preconditions share real orders and retained releases."""

import re
import shlex
import time
from dataclasses import replace

import pytest

from agent_comms.commander import judge, order
from agent_comms.commander.standing import DeliveryAwaitingJudgement
from agent_comms.field_codec import GoalId
from agent_comms.goal_actions import SetGoalAction
from agent_comms.heartbeat_frame import HeartbeatFrame
from agent_comms.held_orders import LiveHold
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.standing_rules import LinkedUnreviewedHead, OrderlessUnreviewedHead, ReviewTimerUp
from agent_comms.todos import (
    Assignment, ClaimOrder, HeadReviewOrder, LinkedHeadReview, NoHeadOrder, OrderClaim,
    OrderHeadReviewRecord, OrderMessage, ProgressRead, RecordOrderHeadVerdict, RecordPullRequest,
    PullRequestLink, ReviewedHeadVerdict, SendBackStrike, StrikeReason, UnderReviewHeadVerdict, VerdictReason,
)

pytest_plugins = ("test_head_review",)

REASON = VerdictReason("The existing class owns this behavior without another copy")
ROAST = "This is stupid because the same decision is written twice instead of belonging to its existing class."
FIX = "Put the shared behavior on the existing parent and delete both copies."
QUOTE = "Repair the existing owner"


@pytest.fixture
def board_review(head_bus):
    comms, github, request = head_bus
    lead = comms.registry.require("lead").incarnation
    worker = comms.registry.require("worker").incarnation

    def claim():
        todo = comms.goals.todos.get("P2")
        original = comms.messaging.send("lead", "worker", todo.order.instructions(todo)).message
        todo = comms.goals.todos.apply(todo.id, OrderMessage(
            todo.revision, lead, original.message_id, at=RecordedEventTime(time.time())))
        delivery = OrderClaim.for_order(todo, worker, request.pr, request.head,
                                        "Review the broken duplicated decision repair",
                                        original.reference, store=comms.goals.todos)
        ClaimOrder(todo.revision, worker, todo.held().generation, delivery).record(comms.goals, todo.id)
        return comms.goals.todos.get(todo.id)

    def link():
        todo = comms.goals.todos.get("P2")
        RecordPullRequest(todo.revision, worker,
                          PullRequestLink(request.repo, request.pr), todo.held().generation).record(comms.goals, todo.id)
        return comms.goals.todos.get(todo.id)

    return comms, request, claim, link


def author_arguments(command, todo):
    """The board supplies identities; the reviewer still has to author the review."""
    arguments = shlex.split(command)
    verb = arguments[1]
    evidence = f"{todo.order.check.directory / todo.order.owner.file}:{todo.order.owner.line}"
    authored = {
        "ok": ["--why", REASON.text, "--serves", QUOTE],
        "back": ["--why", REASON.text, "--roast", ROAST, "--fix", FIX, "--at", evidence],
        "reviewing": [],
    }
    return arguments[1:] + authored[verb]


def assert_review_preconditions(rule, comms, request):
    ctx = order.Commander.read_root(comms.root)
    todo = comms.goals.todos.get("P2")
    commands = re.findall(r"`([^`]+)`", rule.line())
    judged = []
    for command in commands:
        if not command.startswith("judge "):
            continue
        verb = judge.JudgeVerb.parse(author_arguments(command, todo))
        assert not verb.argument_problems()
        scope = HeadReviewOrder.for_request(ctx.todos(), request, verb.order_link, verb.problem)
        scope.require_reviewer(comms.goals, request, ctx.actor_identity)
        for linked in HeadReviewOrder.holding(ctx.todos(), request):
            operation = RecordOrderHeadVerdict(linked.revision, OrderHeadReviewRecord(
                request, ctx.actor_identity, RecordedEventTime(time.time()),
                verb.head_operation(), verb.order_link(linked)))
            operation.prepare(comms.goals, linked)
        judged.append(verb.verdict)
    assert ReviewedHeadVerdict in judged
    return judged


@pytest.mark.parametrize("claim_it", [False, True])
def test_held_board_commands_take_the_same_verdicts_as_the_order(board_review, claim_it):
    comms, request, claim, link = board_review
    todo = claim() if claim_it else link()
    rules = LinkedUnreviewedHead.scan_reviews(comms.goals, "lead", ProgressRead(()))
    rule, = rules
    assert rule.orders == (todo,)
    assert (UnderReviewHeadVerdict in assert_review_preconditions(rule, comms, request)) == claim_it
    frame = HeartbeatFrame.read(comms, "lead", ProgressRead(()))
    # The timer's own condition remains real: age the observed review timer in this read.
    timed = replace(todo, review_at=RecordedEventTime(time.time() - 120))
    frame = replace(frame, live=(LiveHold(timed, comms.registry.require("worker")),))
    timer, = ReviewTimerUp.scan_observed(comms, "lead", (), frame)
    assert_review_preconditions(timer, comms, request)
    for delivery in DeliveryAwaitingJudgement.read_open(
            comms.bus.bus, comms.registry.snapshot(), {todo.held().owner_identity: (todo,)},
            "lead", ()):
        assert_review_preconditions(delivery, comms, request)


def test_orderless_board_commands_do_not_invent_a_problem(board_review):
    comms, request, _, _ = board_review
    rule, = OrderlessUnreviewedHead.scan_reviews(comms.goals, "lead", ProgressRead(()))
    assert rule.orders == ()
    assert "--problem" not in rule.clearing_commands()
    assert UnderReviewHeadVerdict in assert_review_preconditions(rule, comms, request)


def test_claim_link_survives_two_sendbacks_and_released_review_changes_no_goal(board_review):
    comms, request, claim, _ = board_review
    original = claim()
    previous = original.held()
    assert PullRequestLink(request.repo, request.pr) in original.order.pull_requests
    for _ in range(2):
        current = comms.goals.todos.get(original.id)
        SendBackStrike(current.revision, current.creator_identity, current.held(),
                       StrikeReason(ROAST).text, RecordedEventTime(time.time()),
                       evidence=(current.order.owner,)).record(comms.goals, current.id)
    released = comms.goals.todos.get(original.id)
    assert released.current_holder.current_assignments() == ()
    assert released.order.holds_head(request)
    assert comms.goals.todos.resolve_order(released.creator_identity, previous.owner, released.id) == released
    rule, = LinkedUnreviewedHead.scan_reviews(comms.goals, "lead", ProgressRead(()))
    assert UnderReviewHeadVerdict not in assert_review_preconditions(rule, comms, request)
    # A former holder can already be doing different work. A head review must not edit that goal.
    comms.goals.update_goal("worker", SetGoalAction(text="Different work"))
    goal = comms.registry.require("worker").goal.require()
    command = next(command for command in re.findall(r"`([^`]+)`", rule.line())
                   if command.startswith("judge ok "))
    judge.main(author_arguments(command, released), order.Commander.read_root(comms.root))
    accepted = comms.goals.todos.get(original.id)
    assert accepted.order.holds_head(request)
    assert comms.goals.todos.head_reviews(request)[0].order.problem == original.id
    assert comms.registry.require("worker").goal.require() == goal
    proposed = Assignment.of(previous.owner_identity, previous.parent_identity, GoalId.mint())
    reassigned = accepted.assigned(proposed, accepted.order.owner)
    assert reassigned.order.pull_requests == accepted.order.pull_requests
