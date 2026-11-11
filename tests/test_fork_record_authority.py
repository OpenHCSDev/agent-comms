"""A fork answers and records for the teammate it forked from (10-30: 465 owed directives
refused every answer, and a fork that recorded only verdicts could not stop)."""

from dataclasses import replace

import pytest

from agent_comms.cli_bridges.claude_protocol import (
    ClaudeUserHeader,
    HumanPrompt,
    TaskNotification,
    TaskUserRecord,
)
from agent_comms.errors import UnreceivedAnswerError
from agent_comms.messaging.channel_targets import CommandTarget, TeammatesTag
from agent_comms.messaging.sending import TeamReceipts
from agent_comms.planning.order_words import VerdictReason
from agent_comms.planning.orders import (
    NoHeadOrder,
    ReviewedHeadVerdict,
    VerdictAudience,
)
from agent_comms.planning.owner_requests import RequestDirective
from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.head_review import PullRequestHead
from agent_comms.registry.thread_model import DeclaredModel
from agent_comms.registry.threads import ExternalThread, SubagentThread
from agent_comms.team.order import TeamContext
from agent_comms.team.standing_rules import DirectiveUnacted
from agent_comms.transport.tool_caller import ForkCaller, PeerCaller
from test_forks import command, fork_bus  # noqa: F401  (fixture)

REASON = VerdictReason("The existing class owns this behavior without another copy")


def test_shared_record_maintenance_does_not_make_a_successor_the_private_recipient(fork_bus):
    comms, source, _ = fork_bus
    comms.registry.declare(ExternalThread(
        "successor", frozenset({"comms", TeammatesTag.declared_name}), Checkout(source.checkout),
        model=DeclaredModel("anthropic/claude-opus-4-6")))
    retired = comms.registry.require("retained").incarnation
    comms.goals.todos.add_request(retired, "The retired teammate's request")
    ask = comms.messaging.send_user_message(
        CommandTarget.named("retained"), "Ship the retired teammate's fix",
        worktree=Checkout(source.checkout))
    ctx = TeamContext.read_root(comms.root).model_copy(update={"actor": PeerCaller("successor")})
    with pytest.raises(UnreceivedAnswerError, match=f"successor never received {ask.message_id}"):
        comms.messaging.send("successor", "colleague", "Answered", answers=(ask.reference,),
                             answerer=TeamReceipts(ctx.actor_identity, (ctx.incarnation,)))
    with pytest.raises(UnreceivedAnswerError, match=f"successor never received {ask.message_id}"):
        comms.messaging.send("successor", "colleague", "Answered", answers=(ask.reference,),
                             answerer=ctx.receipts)
    reply = comms.messaging.send("retained", "colleague", "Answered", answers=(ask.reference,),
                                 answerer=TeamReceipts(retired, (retired,))).message
    assert comms.bus.bus.lookup(reply.seq).answers == (ask.reference,)


def test_retained_owners_private_directive_does_not_become_every_peers_debt(fork_bus):
    comms, source, _ = fork_bus
    for name in ("peer-one", "peer-two"):
        comms.registry.declare(ExternalThread(
            name, frozenset({"comms", TeammatesTag.declared_name}), Checkout(source.checkout),
            model=DeclaredModel("anthropic/claude-opus-4-6")))
    retired = comms.registry.require("retained").incarnation
    request = comms.goals.todos.add_request(retired, "The retired teammate's request")
    ask = comms.messaging.send_user_message(
        CommandTarget.named("retained"), "Ship the retired teammate's fix",
        worktree=Checkout(source.checkout))
    comms.goals.todos.record_directive(
        request.id, RequestDirective(ask.seq, ask.timestamp + RequestDirective.window))
    owed = lambda name: {rule.message.message_id for rule in DirectiveUnacted.opened(comms, comms.registry.require(name).incarnation)}
    assert ask.message_id not in owed("peer-one") | owed("peer-two")
    assert ask.message_id in owed("retained")
    ctx = TeamContext.read_root(comms.root).model_copy(
        update={"actor": PeerCaller("peer-one")})
    with pytest.raises(UnreceivedAnswerError, match=f"peer-one never received {ask.message_id}"):
        comms.messaging.send("peer-one", "colleague", "Answered", answers=(ask.reference,),
                             answerer=ctx.receipts)
    assert ask.message_id in owed("retained")
    comms.messaging.send("retained", "colleague", "Answered", answers=(ask.reference,),
                         answerer=TeamReceipts(retired, (retired,)))
    assert ask.message_id not in owed("retained")


@pytest.mark.parametrize("fork_count", (1, 3))
def test_channel_delivery_excludes_forks_but_keeps_rosters_and_team_answers(fork_bus, fork_count):
    comms, _, _ = fork_bus
    parent = comms.registry.require("colleague")
    comms.registry.register(replace(
        parent, tags=parent.tags | {TeammatesTag.declared_name}))
    comms.registry.register_child("slot", "colleague", SubagentThread, comms.goals)
    forks = tuple(ForkCaller(f"review-{index}") for index in range(fork_count))
    for fork in forks:
        fork.join(comms, "colleague")

    snapshot = comms.registry.snapshot()
    for fork in forks:
        assert snapshot.require(fork.name).tags == snapshot.require("colleague").tags
        for channel in ("#comms", TeammatesTag.channel()):
            roster = CommandTarget.named(channel).resolve(snapshot, comms.bus.require_channel)
            assert fork.name in {member.thread.name for member in roster.members}

    ask = comms.messaging.send("worker", "#comms", "Review the owner change").message
    delivery = comms.bus.bus.delivery(ask.seq)
    assert {row.canonical_thread for row in delivery.recipients} == {
        "colleague", "other", "retained", "slot"}
    for fork in forks:
        # A fresh channel post gives the fork no receipt of its own.
        with pytest.raises(UnreceivedAnswerError):
            comms.messaging.send(fork.name, "worker", "Without teammate authority",
                                 answers=(ask.reference,))
        context = TeamContext.read_root(comms.root).model_copy(update={"actor": fork})
        reply = context.published(VerdictAudience(("worker",)), "Reviewed the owner",
                                  answers=(ask.reference,))
        assert reply.sender == fork.name
        assert comms.bus.bus.lookup(reply.seq).answers == (ask.reference,)

    # Excluding channel copies must not remove the fork's direct address.
    direct = comms.messaging.send("worker", forks[0].name, "A direct fork request").message
    assert tuple(row.canonical_thread for row in comms.bus.bus.delivery(direct.seq).recipients) == (
        forks[0].name,)


def test_a_fork_whose_record_is_a_head_verdict_may_stop(fork_bus):
    comms, source, _ = fork_bus
    ForkCaller("fork-verdict").join(comms, "colleague")
    refused = command(comms, "order", "retire", "--fork", "fork-verdict")
    assert refused.returncode != 0 and "recorded nothing in Core" in refused.stderr
    fork = comms.registry.require("fork-verdict").incarnation
    NoHeadOrder().record_review(comms.goals, PullRequestHead(source.github, 1, "a" * 40),
                                ReviewedHeadVerdict(REASON), fork)
    retired = command(comms, "order", "retire", "--fork", "fork-verdict")
    assert retired.returncode == 0, retired.stderr


def test_a_task_notification_is_never_owner_words():
    header = ClaudeUserHeader.model_validate(
        {"type": "user", "promptId": "p", "origin": {"kind": TaskNotification.declared_name}})
    assert header.record_type() == TaskUserRecord.declared_name
    text = "<task-notification><task-id>x</task-id></task-notification>"
    assert TaskNotification(kind=TaskNotification.declared_name).owner_words(text) == ()
    assert HumanPrompt().owner_words("ship it") == ("ship it",)
