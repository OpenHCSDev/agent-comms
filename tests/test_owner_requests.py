"""One request family journey through the real store and publication tools."""

from pathlib import Path

import pytest

from agent_comms.change_report import FileLine, MainRevision
from agent_comms.comms import Comms
from agent_comms.connector import CALLER, ConnectorSendTool
from agent_comms.errors import ToolRefusedError
from agent_comms.goals import OrderCheck
from agent_comms.order_runs import MechanicalOrderRun
from agent_comms.messages import MessageReference
from agent_comms.owner_requests import NewestOpenRequest
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repo import Repo
from agent_comms.repository_name import RepositoryName
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.tool_caller import ThreadCaller
from agent_comms.todos import ClaimOrder, OrderClaim, PendingProblemOrder, TodoStore, TriageScope, WithdrawTodo
from agent_comms.tools import CommsSendTool


def test_independent_requests_orders_and_directives(comms: Comms) -> None:
    repository = Repo(checkout=Path.cwd(), github=RepositoryName("OpenHCSDev", "agent-comms"))
    for name in ("claude-ai", "commander", "worker"):
        thread = comms.registry.new_thread(name, frozenset(), str(repository.checkout))
        comms.registry.declare(thread, StoppedThreadStatus())
    commander = comms.registry.require("commander")
    worker = comms.registry.require("worker")
    store = comms.goals.todos
    first = store.add_request(commander.incarnation, "commander native in Core")
    second = store.add_request(commander.incarnation, "virtual bus")
    assert NewestOpenRequest().for_order(store, commander.incarnation) == second.id
    assert first.id.for_order(store, commander.incarnation) == first.id
    assert [request.id for request in store.open_requests(
        commander.incarnation.renamed("commander-renamed"),
    )] == [second.id, first.id]

    order = PendingProblemOrder(
        owner=FileLine.class_declaration(TodoStore, repository, MainRevision()),
        scope=TriageScope(),
        check=OrderCheck(directory=str(repository.checkout), command="true"),
        edits=("Read the request owner and its consumers.",),
        banned=("Leave the saved bus and its sessions unchanged.",),
        report="Report the result of the source investigation.", run=MechanicalOrderRun(),
    )
    todo = store.create(
        "P1", repository.github, "Inspect the request owner",
        creator=commander, owner=worker, generation="request-family",
        review_at=RecordedEventTime(order.at.recorded_at("Order") + 60),
        order=order, request=first.id,
    )
    assert store.serving_orders(first.id) == (todo,)
    assert not store.serving_orders(second.id)
    with pytest.raises(ValueError):
        store.complete_request(commander.incarnation, first.id, "The work is finished.")
    assert len(store.open_requests(commander.incarnation)) == 2
    assert store.serving_orders(first.id) == (todo,)

    ConnectorSendTool(
        sender="claude-ai", target="commander", request=first.id,
        body="Read the request records and report the result.",
    ).apply(comms, CALLER)
    named = comms.bus.log.latest_sequence()
    ConnectorSendTool(
        sender="claude-ai", target="commander",
        body="Read the stored orders and report the result.",
    ).apply(comms, CALLER)
    newest = comms.bus.log.latest_sequence()
    first, second = store.owner_request(first.id), store.owner_request(second.id)
    assert len(first.directives) == len(second.directives) == 1
    assert first.directives[0].original(comms.goals).seq == named
    assert second.directives[0].original(comms.goals).seq == newest
    prefix = first.at_revision(first.directives)
    assert prefix.directives(store) == first.directives
    original = first.directives[0].original(comms.goals)
    claim = OrderClaim.for_order(
        todo, worker.incarnation, 1, "a" * 40, "The request owner was inspected.",
        MessageReference(original.seq, original.message_id), store=store,
    )
    assert claim.message.target == first.commander.name
    assert claim.message.answers == (original.reference,)
    comms.messaging.send(
        "commander", "claude-ai", "The request was read.", answers=(original.reference,),
    )
    assert not first.directives[0].overdue(
        comms.goals, commander.incarnation.renamed("commander-renamed"),
        first.directives[0].deadline + 1,
    )

    CommsSendTool(
        sender="worker", target="commander", body="The source records are available.",
    ).apply(comms, ThreadCaller("worker"))
    assert store.owner_request(first.id).directives == first.directives
    assert store.owner_request(second.id).directives == second.directives
    before = comms.bus.log.latest_sequence()
    with pytest.raises(ValueError):
        ConnectorSendTool(
            sender="claude-ai", target="#any", request=first.id,
            body="Read the request records and report the result.",
        ).apply(comms, CALLER)
    with pytest.raises(ToolRefusedError):
        ConnectorSendTool(
            sender="claude-ai", target="worker", request=first.id,
            body="Read the request records and report the result.",
        ).apply(comms, CALLER)
    assert comms.bus.log.latest_sequence() == before

    store.complete_request(commander.incarnation, second.id, "The independent request is finished.")
    assert [request.id for request in store.open_requests(commander.incarnation)] == [first.id]
    store.apply(todo.id, WithdrawTodo(
        todo.revision, commander.incarnation, todo.held(), "The isolated request check is complete.",
    ))
    store.complete_request(commander.incarnation, first.id, "No pending order remains.")
    assert not store.open_requests(commander.incarnation)

    awaiting = store.add_request(commander.incarnation, "Wait for the commander's delivery verdict.")
    awaiting_order = order.reissued(order.at.recorded_at("Order") + 1)
    awaiting_todo = store.create(
        "P2", repository.github, "Review the holder's delivery claim", creator=commander,
        owner=worker, generation="claim-review", request=awaiting.id,
        review_at=RecordedEventTime(awaiting_order.at.recorded_at("Order") + 60), order=awaiting_order,
    )
    awaiting_claim = OrderClaim.for_order(
        awaiting_todo, worker.incarnation, 2, "b" * 40, "The request's work is ready for review.",
        original.reference, store=store,
    )
    claimed = store.apply(awaiting_todo.id, ClaimOrder(
        awaiting_todo.revision, worker.incarnation, awaiting_todo.held().generation, awaiting_claim,
    ))
    assert store.serving_orders(awaiting.id) == (claimed,)
    with pytest.raises(ValueError):
        store.complete_request(commander.incarnation, awaiting.id, "The claim is not yet accepted.")
    assert store.for_request(first.id)[0].request == first.id
    assert comms.registry.require("commander").goal == commander.goal
    assert not any(thread.process_alive for thread in comms.registry.all_threads().values())
