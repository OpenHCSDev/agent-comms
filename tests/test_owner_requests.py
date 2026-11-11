"""One request family journey through the real store and publication tools."""

from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.comms import Comms
from agent_comms.transport.connector import ConnectorSendTool
from agent_comms.errors import RelationViolationError
from agent_comms.messaging.channel_targets import TeammatesTag
from agent_comms.nominal.field_codec import GoalId
from agent_comms.planning.goals import OrderCheck
from agent_comms.planning.order_runs import MechanicalOrderRun
from agent_comms.planning.orders import (
    ClaimOrder,
    OrderClaim,
    PendingProblemOrder,
    TodoStore,
    TriageScope,
    WithdrawTodo,
)
from agent_comms.planning.owner_requests import NewestOpenRequest
from agent_comms.planning.pull_requests import PullRequestLink
from agent_comms.projects.change_report import FileLine, MainRevision
from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.repo import Repo
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.registry.registry_document import RegisteredTeamProject
from agent_comms.registry.thread_status import StoppedThreadStatus
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.recorded_time import RecordedEventTime
from agent_comms.team.order import Team
from agent_comms.team.request import main as request_main
from agent_comms.team.standing import RequestStalled
from agent_comms.team.standing_rules import DirectiveUnacted
from agent_comms.transport.connector_caller import CALLER, DirectorCaller
from agent_comms.transport.tool_caller import ThreadCaller
from agent_comms.transport.tools.send import CommsSendTool


def test_retired_team_requests_follow_peer_authority(
    comms: Comms, capsys: pytest.CaptureFixture[str],
) -> None:
    repository = Repo(checkout=Path.cwd(), github=RepositoryName("OpenHCSDev", "agent-comms"))
    for name, tags in (
        ("request-author", frozenset({TeammatesTag.declared_name})),
        ("request-peer", frozenset({TeammatesTag.declared_name})),
        ("request-outsider", frozenset()),
    ):
        comms.registry.declare(
            NativeThread(name, tags, Checkout(repository.checkout)), StoppedThreadStatus(),
        )
    # Standing rules read the team; a bus without a team project has no board to stall on.
    comms.registry.set_team_project(RegisteredTeamProject((repository,)))
    store = comms.goals.todos
    author = comms.registry.require("request-author")
    peer = comms.registry.require("request-peer")
    outsider = comms.registry.require("request-outsider")
    first = store.add_request(author.acting_identity, "Retain the original request author.")
    own = store.add_request(outsider.acting_identity, "The outsider's own request.")
    newest = store.add_request(author.acting_identity, "A later author request.")
    comms.registry.update_thread(author.name, lambda thread: replace(thread, tags=frozenset()))
    assert store.owner_request(first.id).teammate == author.incarnation
    assert store.open_requests(peer.acting_identity) == (newest, own, first)
    assert store.open_requests(peer.acting_identity, limit=1, descending=False) == (first,)
    assert store.open_requests(outsider.acting_identity, limit=1) == (newest,)
    assert not store.open_requests(peer.acting_identity, limit=0)
    assert first.id.for_order(store, peer.acting_identity) == first.id
    stalled = [rule.request.id for rule in RequestStalled.violations(comms)]
    assert first.id in stalled
    # 2026-11-04: 8 requests printed once per teammate made a board of 104 rows.
    assert len(stalled) == len(set(stalled))
    peer_context = Team.on_root(peer.name, (repository,), comms.root)
    request_main(["list"], peer_context)
    assert first.id.value in capsys.readouterr().out
    outsider_context = Team.on_root(outsider.name, (repository,), comms.root)
    request_main(
        ["complete", first.id.value, "--why", "The untagged peer completed the request."],
        outsider_context,
    )
    assert f"{first.id.value} done" in capsys.readouterr().out
    assert store.open_requests(peer.acting_identity) == (newest, own)
    assert first.id not in {
        rule.request.id for rule in RequestStalled.violations(comms)
    }
    assert store.owner_request(first.id).teammate == author.incarnation


def test_shared_request_maintenance_does_not_transfer_directive_debt(comms: Comms) -> None:
    """A peer can maintain the request without owing its owner's private directive."""
    checkout = Checkout(Path.cwd())
    teammates = frozenset({TeammatesTag.declared_name})
    for name, tags in (("author", teammates), ("peer", teammates), ("claude-code", frozenset())):
        comms.registry.declare(NativeThread(name, tags, checkout), StoppedThreadStatus())
    author = comms.registry.require("author")
    store = comms.goals.todos
    request = store.add_request(author.acting_identity, "One request.")
    ConnectorSendTool(target="author", request=request.id, body="Do the one request.").apply(
        comms, DirectorCaller("claude-code"))
    directive, = store.owner_request(request.id).directives
    with comms.bus.bus.reading() as source:
        original = directive.original(source)
    assert store.open_requests(comms.registry.require("peer").acting_identity) == (
        store.owner_request(request.id),)
    assert DirectiveUnacted.opened(comms, comms.registry.require("peer").incarnation) == ()
    assert {rule.message.reference for rule in DirectiveUnacted.opened(comms, comms.registry.require("author").incarnation)} == {
        original.reference}
    comms.messaging.send("author", "claude-code", "Done.", answers=(original.reference,))
    assert DirectiveUnacted.opened(comms, comms.registry.require("author").incarnation) == ()
    assert DirectiveUnacted.opened(comms, comms.registry.require("peer").incarnation) == ()


def test_independent_requests_orders_and_directives(comms: Comms, durable_home, capsys) -> None:
    claim_review_goal_id = GoalId.mint()
    request_family_goal_id = GoalId.mint()
    repository = Repo(checkout=Path.cwd(), github=RepositoryName("OpenHCSDev", "agent-comms"))
    for name in ("claude-ai", "colleague", "worker"):
        thread = NativeThread(name, frozenset(), Checkout(repository.checkout))
        comms.registry.declare(thread, StoppedThreadStatus())
    colleague = comms.registry.require("colleague")
    worker = comms.registry.require("worker")
    store = comms.goals.todos
    ctx = Team.on_root("colleague", (repository,), comms.root)
    request_main(["add", "teammate native in Core"], ctx)
    first_id = capsys.readouterr().out.strip()
    request_main(["add", "virtual bus"], ctx)
    second_id = capsys.readouterr().out.strip()
    second, first = store.open_requests(colleague.incarnation)
    assert (first.id.value, second.id.value) == (first_id, second_id)
    assert NewestOpenRequest().for_order(store, colleague.incarnation) == second.id
    assert first.id.for_order(store, colleague.incarnation) == first.id
    assert [
        request.id
        for request in store.open_requests(
            colleague.incarnation.renamed("colleague-renamed"),
        )
    ] == [second.id, first.id]

    order = PendingProblemOrder(
        owner=FileLine.class_declaration(TodoStore, repository, MainRevision()),
        scope=TriageScope(),
        check=OrderCheck(directory=repository.checkout, command="true"),
        edits=("Read the request owner and its consumers.",),
        banned=("Leave the saved bus and its sessions unchanged.",),
        report="Report the result of the source investigation.",
        run=MechanicalOrderRun(),
    )
    todo = store.create(
        "P1",
        repository.github,
        "Inspect the request owner",
        creator=colleague,
        assigner=colleague.incarnation,
        owner=worker,
        generation=request_family_goal_id,
        review_at=RecordedEventTime(order.at.recorded_at("Order") + 60),
        order=order,
        request=first.id,
    )
    assert store.serving_orders(first.id) == (todo,)
    assert not store.serving_orders(second.id)
    request_main(["list"], ctx)
    listed = capsys.readouterr().out
    assert all(
        text in listed
        for text in (first_id, second_id, "P1", "teammate native in Core", "virtual bus")
    )
    with pytest.raises(ValueError):
        request_main(["complete", first_id, "--why", "The work is finished."], ctx)
    assert len(store.open_requests(colleague.incarnation)) == 2
    assert store.serving_orders(first.id) == (todo,)

    ConnectorSendTool(
        target="colleague",
        request=first.id,
        body="Read the request records and report the result.",
    ).apply(comms, CALLER)
    named = comms.bus.bus.revision()
    ConnectorSendTool(
        target="colleague",
        body="Read the stored orders and report the result.",
    ).apply(comms, CALLER)
    newest = comms.bus.bus.revision()
    first, second = store.owner_request(first.id), store.owner_request(second.id)
    assert len(first.directives) == len(second.directives) == 1
    with comms.bus.bus.reading() as source:
        original = first.directives[0].original(source)
        assert original.seq == named
        assert second.directives[0].original(source).seq == newest
    prefix = first.at_revision(first.directives)
    prefix.validate({first.id: first})
    with pytest.raises(RelationViolationError):
        replace(prefix, directive_count=len(first.directives) + 1).validate({first.id: first})
    claim = OrderClaim.for_order(
        todo,
        worker.incarnation,
        PullRequestLink(todo.repo, 1),
        "a" * 40,
        "The request owner was inspected.",
        (original.reference,),
    )
    assert claim.message.target == first.teammate.name
    assert claim.message.answers == (original.reference,)
    comms.messaging.send(
        "colleague",
        "claude-ai",
        "The request was read.",
        answers=(original.reference,),
    )
    renamed = replace(first, teammate=colleague.incarnation.renamed("colleague-renamed"))
    with comms.bus.bus.reading() as source:
        assert not renamed.directive_messages(source, colleague.incarnation.require_recorded().lookup)

    CommsSendTool(
        target="colleague",
        body="The source records are available.",
    ).apply(comms, ThreadCaller("worker"))
    assert store.owner_request(first.id).directives == first.directives
    assert store.owner_request(second.id).directives == second.directives
    before = comms.bus.bus.revision()
    with pytest.raises(ValueError):
        ConnectorSendTool(
            target="#any",
            request=first.id,
            body="Read the request records and report the result.",
        ).apply(comms, CALLER)
    assert comms.bus.bus.revision() == before
    ConnectorSendTool(
        target="worker",
        request=first.id,
        body="Read the request records and report the result.",
    ).apply(comms, CALLER)
    directed = store.owner_request(first.id).directives[-1]
    with comms.bus.bus.reading() as source:
        assert directed.original(source).target == "worker"

    request_main(["complete", second_id, "--why", "The independent request is finished."], ctx)
    capsys.readouterr()
    request_main(["list"], ctx)
    remaining = capsys.readouterr().out
    assert first_id in remaining and "P1" in remaining
    assert second_id not in remaining
    assert [request.id for request in store.open_requests(colleague.incarnation)] == [first.id]
    store.apply(
        todo.id,
        WithdrawTodo(
            todo.revision,
            worker.incarnation,
            "The isolated request check is complete.",
        ),
    )
    store.complete_request(colleague.incarnation, first.id, "No pending order remains.")
    assert not store.open_requests(colleague.incarnation)

    awaiting = store.add_request(
        colleague.incarnation, "Wait for the teammate's delivery verdict."
    )
    awaiting_order = order.reissued(order.at.recorded_at("Order") + 1)
    awaiting_todo = store.create(
        "P2",
        repository.github,
        "Review the holder's delivery claim",
        creator=colleague,
        assigner=colleague.incarnation,
        owner=worker,
        generation=claim_review_goal_id,
        request=awaiting.id,
        review_at=RecordedEventTime(awaiting_order.at.recorded_at("Order") + 60),
        order=awaiting_order,
    )
    awaiting_claim = OrderClaim.for_order(
        awaiting_todo,
        worker.incarnation,
        PullRequestLink(awaiting_todo.repo, 2),
        "b" * 40,
        "The request's work is ready for review.",
        (original.reference,),
    )
    claimed = store.apply(
        awaiting_todo.id,
        ClaimOrder(
            awaiting_todo.revision,
            worker.incarnation,
            awaiting_todo.held().generation,
            awaiting_claim,
        ),
    )
    assert store.serving_orders(awaiting.id) == (claimed,)
    with pytest.raises(ValueError):
        store.complete_request(colleague.incarnation, awaiting.id, "The claim is not yet accepted.")
    assert store.for_request(first.id)[0].request == first.id
    assert comms.registry.require("colleague").goal == colleague.goal
    assert not any(thread.process_alive for thread in comms.registry.all_threads().values())
