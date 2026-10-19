"""Exercise real Core records on an isolated bus; never start or replay a model turn.

Run setup, perform a real tool edit in the owning native session, then run record with that
session path. All observations below are CLI tool results, not hand-constructed events.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from agent_comms.change_report import FileLine
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import CreateProblemGoalAction, SetGoalAction
from agent_comms.goals import CountCheckResult, OrderCheck, RequestGoalOrder
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.tools import (CommsFireTool, CommsOrderDoneTool, CommsTodoCreateTool,
                              CommsTodoTool, CommsVerdictTool)
from agent_comms.todos import (AcceptedVerdict, CommitDelivery, MechanicalOrderRun,
                              PendingProblemOrder, StrikeReason, StrikeTodo, VoidStrikeTodo)
from agent_comms.progress_claims import MergedClaim

CHECKOUT = Path(__file__).resolve().parents[2]
ROOT = CHECKOUT / ".proof-record-plane"
RECEIPT = CHECKOUT / "docs/evidence/cn-phase3-record-results.jsonl"


def cli(caller, tool):
    arguments = FieldCodec.encode(tool)
    del arguments[tool.family_discriminator]
    command = [sys.executable, "-m", "agent_comms.cli", "--root", str(ROOT), "tool",
               "--from", caller, "--tool", tool.declared_name,
               "--arguments", json.dumps(arguments)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=180,
                            env=os.environ | {"PYTHONPATH": str(CHECKOUT / "src")})
    print(result.stdout, end="", flush=True)
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    with RECEIPT.open("a") as output:
        output.write(result.stdout)
    return json.loads(result.stdout)


def setup():
    if ROOT.exists():
        raise RuntimeError(f"Refusing to reuse an existing proof root: {ROOT}")
    comms = Comms(ROOT)
    comms.messaging.initialize_private_initial_protocol()
    for name in ("record-lead", "record-worker"):
        comms.registry.declare(comms.registry.new_thread(
            name, frozenset(), str(CHECKOUT), created_at=time.time()))
    comms.goals.update_goal("record-lead", SetGoalAction(
        text="Confirm the order record through the actual Core tools on this isolated bus",
        goal_id="record-request", order=RequestGoalOrder()))
    comms.goals.update_goal("record-worker", SetGoalAction(
        text="Complete the assigned record-plane proof without touching any live session",
        goal_id="record-work"))
    create_order()


def create_order(problem="P1"):
    now = time.time()
    cli("record-lead", CommsTodoCreateTool(owner="record-worker", order=CreateProblemGoalAction(
        problem=problem, repo="OpenHCSDev/agent-comms",
        text="Show the real verdict and strike records without a second source",
        request="record-request", commander="record-lead",
        deadline=RecordedEventTime(now + 3600), order=PendingProblemOrder(
            id=f"record-order-{problem}", owner=FileLine(str(CHECKOUT / "src/agent_comms/todos.py"), 69),
            check=OrderCheck(str(CHECKOUT), "printf 1", CountCheckResult(1)),
            edits=("Decode pull request target branches through the existing record owner",),
            banned=("No second order store",), report="Report the actual Core tool response",
            run=MechanicalOrderRun(), at=RecordedEventTime(now),
            first_call_deadline=RecordedEventTime(now + 120)))))


def record(session: Path):
    from dataclasses import replace
    from agent_comms.turn_digests import OrderEditsRead, TurnsSince

    retained = ROOT / "record-worker.jsonl"
    shutil.copy2(session, retained)
    comms = Comms(ROOT)
    worker = comms.registry.require("record-worker")
    comms.registry.register(replace(worker, session_file=str(retained)),
                            comms.registry.status(worker.name))
    todo = comms.goals.todos.get("P1")
    since = todo.order.at.recorded_at("An order")
    calls = OrderEditsRead(ROOT, comms.registry.require(worker.name), TurnsSince(since), todo).read()
    if not calls:
        raise RuntimeError("No actual native edit call followed the order; do not invent one")
    judged = calls[0].id
    cli("record-lead", CommsVerdictTool(id="P1", operation=AcceptedVerdict(
        todo.revision, comms.registry.require("record-lead").incarnation, todo.held(), judged,
        quote="Decode pull request target branches")))
    for reason in ("The assigned change left the target branch out of the saved order record",
                   "The second review found the target branch still missing from the saved record"):
        todo = comms.goals.todos.get("P1")
        cli("record-lead", CommsTodoTool(id="P1", operation=StrikeTodo(
            todo.revision, comms.registry.require("record-lead").incarnation, todo.held(), reason)))
    todo = comms.goals.todos.get("P1")
    assert todo.struck_out(worker.name)
    cli("record-lead", CommsFireTool(unit=worker.name, reason=StrikeReason(
        "The unit left the required target branch absent after both recorded reviews")))
    todo = comms.goals.todos.get("P1")
    cli("record-lead", CommsTodoTool(id="P1", operation=VoidStrikeTodo(
        todo.revision, comms.registry.require("record-lead").incarnation, todo.strikes[-1],
        "The second strike repeated the first finding rather than describing another failed change")))
    todo = comms.goals.todos.get("P1")
    assert len(todo.strikes) == 2 and len(todo.voided) == 1
    assert len(todo.live_strikes(worker.name)) == 1 and todo.assignment is None


def deliver(problem: str, delivered: str, checked: str | None):
    create_order(problem)
    checked = checked or subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=CHECKOUT, text=True).strip()
    cli("record-lead", CommsOrderDoneTool(
        problem=problem, order_id=f"record-order-{problem}",
        delivery=CommitDelivery(commit=checked, delivered_commit=delivered),
        claims=(MergedClaim(delivered),)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("step", choices=("setup", "order", "record", "deliver"))
    parser.add_argument("--session", type=Path)
    parser.add_argument("--problem")
    parser.add_argument("--delivered")
    parser.add_argument("--checked")
    args = parser.parse_args()
    if args.step == "setup":
        setup()
    elif args.step == "order":
        create_order()
    elif args.step == "deliver":
        deliver(args.problem, args.delivered, args.checked)
    else:
        record(args.session)
