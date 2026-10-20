"""Exercise real Core records on an isolated bus; never start or replay a model turn.

Run setup, perform a real tool edit in the owning native session, then run record with that
session path. All observations below are CLI tool results, not hand-constructed events.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

from agent_comms import cli as comms_cli
from agent_comms.change_report import FileLine
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import CreateProblemGoalAction, SetGoalAction
from agent_comms.goals import CountCheckResult, OrderCheck, RequestGoalOrder
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.todos import (
    AcceptedVerdict,
    CommitDelivery,
    MechanicalOrderRun,
    PendingProblemOrder,
    StrikeReason,
    StrikeTodo,
    VoidStrikeTodo,
)
from agent_comms.tool_caller import ToolCaller
from agent_comms.tools import (
    CommsChangeProblemTool,
    CommsFireTool,
    CommsOrderDoneTool,
    CommsTodoCreateTool,
    CommsVerdictTool,
    ToolRequest,
)
from agent_comms.turn_digests import OrderEditsRead, TurnsSince

CHECKOUT = Path(__file__).resolve().parents[2]
ROOT = Path.home() / ".cache" / "agent-scratch" / "pi-cn-phase3" / "record-plane"
RESULTS = ROOT.parent / "cn-phase3-record-results.jsonl"


def cli[Caller: ToolCaller](caller: str, tool: ToolRequest[Caller]) -> str:
    arguments = FieldCodec.encode(tool)
    del arguments[tool.family_discriminator]
    command = [
        sys.executable,
        "-m",
        comms_cli.__name__,
        "--root",
        str(ROOT),
        "tool",
        "--from",
        caller,
        "--tool",
        tool.declared_name,
        "--arguments",
        json.dumps(arguments),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=180,
        env=os.environ | {"PYTHONPATH": str(CHECKOUT / "src")},
    )
    print(result.stdout, end="", flush=True)
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    with RESULTS.open("a") as output:
        output.write(result.stdout)
    return result.stdout


def setup() -> None:
    if ROOT.exists():
        raise RuntimeError(f"Refusing to reuse an existing proof root: {ROOT}")
    comms = Comms(ROOT)
    comms.messaging.initialize_private_initial_protocol()
    for name in ("record-lead", "record-worker"):
        comms.registry.declare(
            comms.registry.new_thread(name, frozenset(), str(CHECKOUT), created_at=time.time())
        )
    comms.goals.update_goal(
        "record-lead",
        SetGoalAction(
            text="Confirm the order record through the actual Core tools on this isolated bus",
            goal_id="record-request",
            order=RequestGoalOrder(),
        ),
    )
    comms.goals.update_goal(
        "record-worker",
        SetGoalAction(
            text="Complete the assigned record-plane proof without touching any live session",
            goal_id="record-work",
        ),
    )
    create_order()


def create_order(problem: str = "P1") -> None:
    now = time.time()
    cli(
        "record-lead",
        CommsTodoCreateTool(
            owner="record-worker",
            order=CreateProblemGoalAction(
                problem=problem,
                repo="OpenHCSDev/agent-comms",
                text="Show the real verdict and strike records without a second source",
                request="record-request",
                commander="record-lead",
                deadline=RecordedEventTime(now + 3600),
                order=PendingProblemOrder(
                    id=f"record-order-{problem}",
                    owner=FileLine(str(CHECKOUT / "src/agent_comms/todos.py"), 69),
                    check=OrderCheck(str(CHECKOUT), "printf 1", CountCheckResult(1)),
                    edits=(
                        "Decode pull request target branches through the existing record owner",
                    ),
                    banned=("No second order store",),
                    report="Report the actual Core tool response",
                    run=MechanicalOrderRun(),
                    at=RecordedEventTime(now),
                    first_call_deadline=RecordedEventTime(now + 120),
                ),
            ),
        ),
    )


def record(session: Path) -> None:
    retained = ROOT / "record-worker.jsonl"
    shutil.copy2(session, retained)
    comms = Comms(ROOT)
    worker = comms.registry.require("record-worker")
    comms.registry.register(
        replace(worker, session_file=str(retained)), comms.registry.status(worker.name)
    )
    todo = comms.goals.todos.get("P1")
    since = todo.order.checked().at.recorded_at("An order")
    calls = OrderEditsRead(
        ROOT, comms.registry.require(worker.name), TurnsSince(since), todo
    ).read()
    if not calls:
        raise RuntimeError("No actual native edit call followed the order; do not invent one")
    judged = calls[0].id
    cli(
        "record-lead",
        CommsVerdictTool(
            problem="P1",
            operation=AcceptedVerdict(
                todo.revision,
                comms.registry.require("record-lead").incarnation,
                todo.held(),
                judged,
                quote="Decode pull request target branches",
            ),
        ),
    )
    for reason in (
        "The assigned change left the target branch out of the saved order record",
        "The second review found the target branch still missing from the saved record",
    ):
        todo = comms.goals.todos.get("P1")
        cli(
            "record-lead",
            CommsChangeProblemTool(
                problem="P1",
                operation=StrikeTodo(
                    todo.revision,
                    comms.registry.require("record-lead").incarnation,
                    todo.held(),
                    reason,
                ),
            ),
        )
    todo = comms.goals.todos.get("P1")
    assert todo.struck_out(worker.name)
    cli(
        "record-lead",
        CommsFireTool(
            unit=worker.name,
            reason=StrikeReason(
                "The unit left the required target branch absent after both recorded reviews"
            ),
        ),
    )
    todo = comms.goals.todos.get("P1")
    cli(
        "record-lead",
        CommsChangeProblemTool(
            problem="P1",
            operation=VoidStrikeTodo(
                todo.revision,
                comms.registry.require("record-lead").incarnation,
                todo.strikes[-1],
                "The second strike repeated the first finding "
                "rather than describing another failed change",
            ),
        ),
    )
    todo = comms.goals.todos.get("P1")
    assert len(todo.strikes) == 2 and len(todo.voided) == 1
    assert len(todo.live_strikes(worker.name)) == 1 and len(todo.released) == 1
    released = todo.released[-1].previous
    assert released.owner_identity == worker.incarnation
    todo.require_released_order(worker.incarnation, released.generation)


def deliver(problem: str, delivered: str, checked: str) -> None:
    create_order(problem)
    cli(
        "record-lead",
        CommsOrderDoneTool(
            problem=problem,
            order_id=f"record-order-{problem}",
            delivery=CommitDelivery(commit=checked, delivered_commit=delivered),
        ),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    steps = parser.add_subparsers(required=True)
    steps.add_parser(setup.__name__).set_defaults(action=lambda _: setup())
    order_parser = steps.add_parser(create_order.__name__)
    order_parser.add_argument("--problem", default="P1")
    order_parser.set_defaults(action=lambda args: create_order(args.problem))
    record_parser = steps.add_parser(record.__name__)
    record_parser.add_argument("--session", required=True, type=Path)
    record_parser.set_defaults(action=lambda args: record(args.session))
    delivery_parser = steps.add_parser(deliver.__name__)
    delivery_parser.add_argument("--problem", required=True)
    delivery_parser.add_argument("--delivered", required=True)
    delivery_parser.add_argument("--checked", required=True)
    delivery_parser.set_defaults(
        action=lambda args: deliver(args.problem, args.delivered, args.checked)
    )
    args = parser.parse_args()
    args.action(args)
