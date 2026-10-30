"""A commander fork answers and records for its commander (10-30: 465 owed directives
refused every answer, and a fork that recorded only verdicts could not stop)."""

import time

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
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.thread_model import DeclaredModel
from agent_comms.threads import Thread
from agent_comms.todos import NoHeadOrder, ReviewedHeadVerdict, VerdictReason
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
