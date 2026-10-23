"""Unit cards from real records, with unread failures isolated to their unit."""

import json
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_comms.commander_heartbeat import CommanderHeartbeat
from agent_comms.launch_runtime import LaunchRoot
from agent_comms.message_page import MessagePageRequest
from agent_comms.read_basis import DMDisplayScope
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.thread_text import ForkedParent, SavedSessionFile
from agent_comms.todos import DeliverTodo, OperationDelivery, PullRequests
from agent_comms.ui_model.unit_state import (
    AwaitingCommander,
    Blocked,
    DeliveredUnclosed,
    Detached,
    Idle,
    Moving,
    Spinning,
    StalledOnNothing,
    Stuck,
    UnitState,
    Unreadable,
)
from test_holder_standing_rules import blocked_holder, publish, state_cards

pytest_plugins = ("test_holder_standing_rules",)


def recorded_turn(comms, calls):
    stamp = datetime.fromtimestamp(time.time(), UTC).isoformat()
    rows = [
        dict(type="session", id="state-source", version=3, timestamp=stamp, cwd=str(comms.root)),
        dict(
            type="message",
            id="input",
            timestamp=stamp,
            message=dict(role="user", content="Repair the existing owner"),
        ),
    ]
    for index in range(calls):
        rows.append(
            dict(
                type="message",
                id=f"call-{index}",
                timestamp=stamp,
                message=dict(
                    role="assistant",
                    content=[
                        dict(
                            type="toolCall",
                            id=f"read-{index}",
                            name="read",
                            arguments=dict(path="owner.py"),
                        )
                    ],
                    stopReason="toolUse",
                ),
            )
        )
        rows.append(
            dict(
                type="message",
                id=f"result-{index}",
                timestamp=stamp,
                message=dict(
                    role="toolResult",
                    toolCallId=f"read-{index}",
                    toolName="read",
                    isError=False,
                    content=[dict(type="text", text="contents")],
                ),
            )
        )
    rows.append(
        dict(
            type="message",
            id="answer",
            timestamp=stamp,
            message=dict(
                role="assistant",
                content=[dict(type="text", text="Continue the repair")],
                stopReason="stop",
            ),
        )
    )
    path = comms.root / "state-source.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)
    thread = comms.registry.require("worker")
    comms.registry.register(replace(thread, session_file=SavedSessionFile(str(path))))


def unread_checkout(comms, tmp_path):
    checkout = tmp_path / "unread-worker"
    checkout.mkdir()
    (checkout / ".git").write_text("gitdir: ./removed-git-directory\n")
    comms.registry.register(replace(comms.registry.require("worker"), worktree=str(checkout)))


@pytest.mark.parametrize("member", UnitState.worst_first(), ids=lambda member: member.__name__)
def test_unit_state_family_uses_real_records(held_bus, tmp_path, member):
    comms = held_bus
    unit = "worker"
    with LaunchRoot.from_path(comms.root).selected():
        if member is Unreadable:
            unread_checkout(comms, tmp_path)
        elif member is Idle:
            unit = "outsider"
        elif member is Blocked:
            blocked_holder(comms)
        elif member is Detached:
            comms.registry.register(comms.registry.require(unit), StoppedThreadStatus())
        elif member is AwaitingCommander:
            publish(comms, unit, "lead", time.time() - AwaitingCommander.seconds - 1)
        elif member is Stuck:
            with comms.goals.todos.writing() as db:
                for todo in comms.goals.todos.held_for_commander(
                    comms.registry.require("lead").incarnation
                ):
                    replace(
                        todo,
                        order=replace(
                            todo.order.checked(),
                            first_call_deadline=RecordedEventTime(time.time() - 1),
                        ),
                    ).upsert(db)
        elif member is Spinning:
            recorded_turn(comms, 2)
        elif member is StalledOnNothing:
            recorded_turn(comms, 1)
        elif member is DeliveredUnclosed:
            for todo in comms.goals.todos.held_for_commander(
                comms.registry.require("lead").incarnation
            ):
                comms.goals.todos.apply(
                    todo.id,
                    DeliverTodo(
                        todo.revision,
                        comms.registry.require("lead").incarnation,
                        OperationDelivery(at=RecordedEventTime(time.time())),
                        todo.order.id,
                    ),
                )
        elif member is Moving:
            pass  # Assigned work, before its first-call deadline.
        else:
            pytest.fail(f"New UnitState needs its real-record scenario: {member}")
        states = UnitState.current(comms, "lead", PullRequests())
        (state,) = (state for state in states if state.unit.name == unit)
        assert isinstance(state, member)
        assert state.unit == comms.registry.require(unit).incarnation
        assert state.default_action and state.unit.name in state.line()
        assert not any(
            isinstance(state, sibling)
            for sibling in UnitState.worst_first()
            if sibling is not member
        )
        assert states == tuple(
            state
            for expected in UnitState.worst_first()
            for state in states
            if isinstance(state, expected)
        )
        print(state.line())


def test_claim_is_a_commander_question_not_a_verified_delivery(held_bus, claim_order):
    comms = held_bus
    claim = claim_order("P1")
    with LaunchRoot.from_path(comms.root).selected():
        (state,) = state_cards(comms, AwaitingCommander)
        (question,) = state.owed
        assert question.message_id == claim.message.message_id
        assert question.seq > claim.message.answers[0].seq
        assert state_cards(comms, DeliveredUnclosed) == ()


def test_stopped_child_without_lead_orders_remains_in_the_frame(held_bus):
    child = replace(held_bus.registry.require("outsider"), parent=ForkedParent("lead"))
    held_bus.registry.register(child, StoppedThreadStatus())
    with LaunchRoot.from_path(held_bus.root).selected():
        (state,) = (
            state
            for state in UnitState.current(held_bus, "lead", PullRequests())
            if state.unit == child.incarnation
        )
        assert isinstance(state, Idle)
        assert state.presence == StoppedThreadStatus()


def test_malformed_native_source_is_unreadable_not_idle(held_bus):
    recorded_turn(held_bus, 1)
    path = held_bus.root / "state-source.jsonl"
    rows = path.read_text().splitlines()
    header = json.loads(rows[0])
    del header["timestamp"]
    path.write_text("\n".join((json.dumps(header), *rows[1:])) + "\n")
    with LaunchRoot.from_path(held_bus.root).selected():
        (state,) = state_cards(held_bus, Unreadable)
        assert state.unit.name == "worker"
        assert "timestamp" in state.reason


@pytest.mark.parametrize(
    "primary,broken_metadata",
    ((False, True), (True, True), (False, False)),
    ids=("home", "order", "nongit-home"),
)
def test_heartbeat_keeps_every_unit_when_one_checkout_is_unreadable(
    held_bus, tmp_path, primary, broken_metadata, installed_runtime
):
    import os

    from agent_comms.child_process import ProcessIdentity

    if primary:
        checkout = Path(held_bus.goals.todos.get("P1").order.checked().check.directory)
        (checkout / ".git").rename(checkout / "saved-git")
        (checkout / ".git").write_text("gitdir: ./removed-git-directory\n")
    else:
        unread_checkout(held_bus, tmp_path)
    if not broken_metadata:
        (held_bus.registry.require("worker").project_path / ".git").unlink()
    lead = held_bus.registry.require("lead")
    held_bus.registry.register(replace(lead, process_identity=ProcessIdentity.capture(os.getpid())))
    heartbeat = CommanderHeartbeat()
    with LaunchRoot.from_path(held_bus.root).selected():
        observed = heartbeat.observe(held_bus)
        cards = observed[0].states
        assert isinstance(cards[0], Unreadable)
        assert {card.unit.name for card in cards} >= {"worker", "outsider"}
        heartbeat.pass_body(held_bus, observed)
        page = held_bus.bus.log.read_page(
            MessagePageRequest(
                DMDisplayScope.capture(
                    heartbeat.sender,
                    "lead",
                    held_bus.registry.snapshot(),
                )
            )
        )
        message = next(row for row in page.messages if row.sender == heartbeat.sender)
        assert all(card.line() in message.body for card in cards)
        assert message.body.index(cards[0].line()) < message.body.index(cards[-1].line())
        print(message.body)
