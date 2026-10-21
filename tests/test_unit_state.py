"""One family journey: each state is derived from actual Core records, not patched methods."""
from dataclasses import replace
from datetime import UTC, datetime
import json
import time

import pytest

from test_holder_standing_rules import held_bus, blocked_holder, publish
from agent_comms.launch_runtime import LaunchRoot
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.thread_text import SavedSessionFile
from agent_comms.todos import DeliverTodo, OperationDelivery
from agent_comms.ui_model.unit_state import (
    AwaitingCommander, Blocked, DeliveredUnclosed, Detached, Idle, Moving,
    Spinning, StalledOnNothing, Stuck, UnitState,
)


def recorded_turn(comms, calls):
    now = time.time()
    stamp = datetime.fromtimestamp(now, UTC).isoformat()
    rows = [dict(type="session", id="state-source", version=3),
            dict(type="message", id="input", timestamp=stamp,
                 message=dict(role="user", content="Repair the existing owner"))]
    for index in range(calls):
        rows.append(dict(type="message", id=f"call-{index}", timestamp=stamp,
            message=dict(role="assistant", content=[dict(type="toolCall", id=f"read-{index}",
                name="read", arguments=dict(path="owner.py"))], stopReason="toolUse")))
        rows.append(dict(type="message", id=f"result-{index}", timestamp=stamp,
            message=dict(role="toolResult", toolCallId=f"read-{index}", toolName="read",
                isError=False, content=[dict(type="text", text="contents")])) )
    rows.append(dict(type="message", id="answer", timestamp=stamp,
        message=dict(role="assistant", content=[dict(type="text", text="Continue the repair")],
                     stopReason="stop")))
    path = comms.root / "state-source.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)
    thread = comms.registry.require("worker")
    comms.registry.register(replace(thread, session_file=SavedSessionFile(str(path))))


@pytest.mark.parametrize("member", UnitState.members_with(UnitState), ids=lambda member: member.declared_name)
def test_unit_state_family_uses_real_records(held_bus, member):
    comms = held_bus
    unit = "worker"
    with LaunchRoot.from_path(comms.root).selected():
        if member is Idle:
            unit = "outsider"
        elif member is Blocked:
            blocked_holder(comms)
        elif member is Detached:
            comms.registry.register(comms.registry.require(unit), StoppedThreadStatus())
        elif member is AwaitingCommander:
            publish(comms, unit, "lead", time.time() - AwaitingCommander.seconds - 1)
        elif member is Stuck:
            with comms.goals.todos.writing() as db:
                for todo in comms.goals.todos.held_for_commander(comms.registry.require("lead").incarnation):
                    replace(todo, order=replace(todo.order.checked(),
                        first_call_deadline=RecordedEventTime(time.time() - 1))).upsert(db)
        elif member is Spinning:
            recorded_turn(comms, 2)
        elif member is StalledOnNothing:
            recorded_turn(comms, 1)
        elif member is DeliveredUnclosed:
            for todo in comms.goals.todos.held_for_commander(comms.registry.require("lead").incarnation):
                comms.goals.todos.apply(todo.id, DeliverTodo(todo.revision,
                    comms.registry.require("lead").incarnation,
                    OperationDelivery(at=RecordedEventTime(time.time())), todo.order.id))
        elif member is Moving:
            pass  # Freshly assigned work before its first call deadline.
        else:
            pytest.fail(f"New UnitState needs its real-record scenario: {member}")
        source = next(source for source in UnitState.observations(comms, "lead")
                      if source.thread.name == unit)
        state = UnitState.read(source)
        assert type(state) is member
        assert state.unit == comms.registry.require(unit).incarnation
        assert state.default_action and state.unit.name in state.line()
        print(state.line())
