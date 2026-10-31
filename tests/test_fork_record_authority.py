"""A commander fork answers and records for its commander (10-30: 465 owed directives
refused every answer, and a fork that recorded only verdicts could not stop)."""

from dataclasses import replace

import pytest

from agent_comms.channel_targets import CommandersTag, CommandTarget
from agent_comms.cli_bridges.claude_protocol import (
    ClaudeUserHeader, HumanPrompt, TaskNotification, TaskUserRecord,
)
from agent_comms.commander.order import CommanderContext
from agent_comms.errors import UnreceivedAnswerError
from agent_comms.head_review import PullRequestHead
from agent_comms.messaging import CommanderReceipts
from agent_comms.repo import Checkout
from agent_comms.thread_execution import ExternalThreadExecution, SubagentExecution
from agent_comms.thread_model import DeclaredModel
from agent_comms.standing_rules import DirectiveUnacted
from agent_comms.threads import Thread
from agent_comms.todos import NoHeadOrder, RequestDirective, ReviewedHeadVerdict, VerdictAudience, VerdictReason
from agent_comms.tool_caller import CommanderForkCaller, CommanderPeerCaller

from test_commander_forks import command, fork_bus  # noqa: F401  (fixture)

REASON = VerdictReason("The existing class owns this behavior without another copy")


def test_a_successor_answers_directives_its_retired_predecessor_received(fork_bus):
    comms, source, _ = fork_bus
    comms.registry.declare(Thread(
        "successor", frozenset({"comms", CommandersTag.declared_name}), Checkout(source.checkout),
        model=DeclaredModel("anthropic/claude-opus-4-6"), execution=ExternalThreadExecution))
    retired = comms.registry.require("retained").incarnation
    comms.goals.todos.add_request(retired, "The retired commander's request")
    ask = comms.messaging.send_user_message(
        CommandTarget.named("retained"), "Ship the retired commander's fix",
        worktree=Checkout(source.checkout))
    ctx = CommanderContext.read_root(comms.root).model_copy(update={"actor": CommanderPeerCaller("successor")})
    with pytest.raises(UnreceivedAnswerError, match=f"successor never received {ask.message_id}"):
        comms.messaging.send("successor", "lead", "Answered", answers=(ask.reference,),
                             answerer=CommanderReceipts(ctx.actor_identity, (ctx.incarnation,)))
    reply = comms.messaging.send("successor", "lead", "Answered", answers=(ask.reference,),
                                 answerer=ctx.receipts).message
    assert comms.bus.bus.lookup(reply.seq).answers == (ask.reference,)


def test_one_peers_answer_clears_a_retired_commanders_directive_for_every_peer(fork_bus):
    comms, source, _ = fork_bus
    for name in ("peer-one", "peer-two"):
        comms.registry.declare(Thread(
            name, frozenset({"comms", CommandersTag.declared_name}), Checkout(source.checkout),
            model=DeclaredModel("anthropic/claude-opus-4-6"), execution=ExternalThreadExecution))
    retired = comms.registry.require("retained").incarnation
    request = comms.goals.todos.add_request(retired, "The retired commander's request")
    ask = comms.messaging.send_user_message(
        CommandTarget.named("retained"), "Ship the retired commander's fix",
        worktree=Checkout(source.checkout))
    comms.goals.todos.record_directive(
        request.id, RequestDirective(ask.seq, ask.timestamp + RequestDirective.window))
    owed = lambda name: {rule.message.message_id for rule in DirectiveUnacted.opened(comms, name)}
    assert ask.message_id in owed("peer-one") & owed("peer-two")
    ctx = CommanderContext.read_root(comms.root).model_copy(
        update={"actor": CommanderPeerCaller("peer-one")})
    comms.messaging.send("peer-one", "lead", "Answered", answers=(ask.reference,),
                         answerer=ctx.receipts)
    assert ask.message_id not in owed("peer-one") | owed("peer-two")


@pytest.mark.parametrize("fork_count", (1, 3))
def test_channel_delivery_excludes_forks_but_keeps_rosters_and_commander_answers(fork_bus, fork_count):
    comms, _, _ = fork_bus
    parent = comms.registry.require("lead")
    comms.registry.register(replace(
        parent, tags=parent.tags | {CommandersTag.declared_name}))
    comms.registry.register_child("slot", "lead", SubagentExecution, comms.goals)
    forks = tuple(CommanderForkCaller(f"review-{index}") for index in range(fork_count))
    for fork in forks:
        fork.join(comms, "lead")

    snapshot = comms.registry.snapshot()
    for fork in forks:
        assert snapshot.require(fork.name).tags == snapshot.require("lead").tags
        for channel in ("#comms", CommandersTag.channel()):
            roster = CommandTarget.named(channel).resolve(snapshot, comms.bus.require_channel)
            assert fork.name in {member.thread.name for member in roster.members}

    ask = comms.messaging.send("worker", "#comms", "Review the owner change").message
    delivery = comms.bus.bus.delivery(ask.seq)
    assert {row.canonical_thread for row in delivery.recipients} == {
        "lead", "other", "retained", "slot"}
    for fork in forks:
        # A fresh channel post gives the fork no receipt of its own.
        with pytest.raises(UnreceivedAnswerError):
            comms.messaging.send(fork.name, "worker", "Without commander authority",
                                 answers=(ask.reference,))
        context = CommanderContext.read_root(comms.root).model_copy(update={"actor": fork})
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
    CommanderForkCaller("fork-verdict").join(comms, "lead")
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
