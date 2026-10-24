"""Exercise real Core records on an isolated bus; never start or replay a model turn.

Run setup, perform a real tool edit in the owning native session, then run record with that
session path. All observations below are CLI tool results, not hand-constructed events.
"""

from __future__ import annotations



import argparse
import os
import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path

from agent_comms.change_report import FileLine, MainRevision
from agent_comms.repo import Repo
from agent_comms.comms import wire
from agent_comms.goal_actions import CreateProblemGoalAction, SetGoalAction
from agent_comms.goals import CountCheckResult, OrderCheck
from agent_comms.owner_requests import NewestOpenRequest
from agent_comms.progress_claims import MergedClaim
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.todos import (
    AcceptedVerdict,
    CommitDelivery,
    FixScope,
    FixShape,
    PendingProblemOrder,
    StrikeReason,
    StrikeTodo,
    VoidStrikeTodo,
)
from agent_comms.order_runs import MechanicalOrderRun
from agent_comms.threads import Thread
from agent_comms.tool_caller import ToolCaller
from agent_comms.order_tools import CommsChangeProblemTool, CommsFireTool, CommsOrderDoneTool, CommsTodoCreateTool, CommsVerdictTool
from agent_comms.tool_request import ToolRequest
from agent_comms.turn_digests import OrderEditsRead, TurnsSince

CHECKOUT = Path(__file__).resolve().parents[2]
ROOT = Path.home() / ".cache" / "agent-scratch" / "pi-cn-phase3" / "record-plane"
RESULTS = ROOT.parent / "cn-phase3-record-results.jsonl"


def cli[Caller: ToolCaller](caller: str, tool: ToolRequest[Caller]) -> str:
    command = tool.cli_command(ToolCaller.from_actor(caller)).argv(ROOT)
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
    comms = wire(ROOT)
    for name in ("record-lead", "record-worker"):
        comms.registry.declare(
            Thread(name, frozenset(), str(CHECKOUT), created_at=time.time())
        )
    comms.goals.todos.add_request(comms.registry.require('record-lead').incarnation,
        'Confirm the order record through the actual Core tools on this isolated bus')
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
    repo = Repo.of(CHECKOUT)
    comms = wire(ROOT)
    request = NewestOpenRequest().for_order(
        comms.goals.todos, comms.registry.require('record-lead').incarnation)
    cli(
        "record-lead",
        CommsTodoCreateTool(
            owner="record-worker",
            order=CreateProblemGoalAction(
                problem=problem,
                repo=repo.github,
                text="Show the real verdict and strike records without a second source",
                request=request,
                commander="record-lead",
                assigner=wire(ROOT).registry.require("record-lead").incarnation,
                review_at=RecordedEventTime(now + 3600),
                order=PendingProblemOrder(
                    id=f"record-order-{problem}",
                    scope=FixScope(FixShape(parents=(
                        FileLine.class_declaration(PendingProblemOrder, repo, MainRevision()),
                    ), members=())),
                    owner=FileLine(str(CHECKOUT / "src/agent_comms/todos.py"), 69),
                    check=OrderCheck(CHECKOUT, "printf 1", CountCheckResult(1)),
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
    comms = wire(ROOT)
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
            claims=(MergedClaim(delivered),),
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
