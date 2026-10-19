"""CLI scratch-bus journey using a copy of this worker's actual saved session.

No fake Comms, authored transcript events, provider input or live-bus writes.
Run setup, read order_story.py in the actual worker, then run finish.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from agent_comms.change_report import FileLine
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import CreateProblemGoalAction, SetGoalAction
from agent_comms.goals import OrderCheck, RequestGoalOrder
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.todos import MechanicalOrderRun, PendingProblemOrder

PROJECT = Path(__file__).resolve().parents[1]
EVIDENCE = PROJECT / "evidence/phase5"
ROOT = EVIDENCE / "bus"
WORKER = "pi-cn-phase5"
COMMS = Comms(ROOT)


def invoke(name, **arguments):
    run = subprocess.run([sys.executable, "-m", "agent_comms.cli", "--root", str(ROOT),
        "tool", "--from", "lead", "--tool", name, "--arguments", json.dumps(arguments)],
        capture_output=True, text=True, check=True)
    reply = json.loads(run.stdout)
    if reply.get("isError"):
        raise RuntimeError(reply)
    return reply


def show(stage):
    story = invoke("comms_order_story", problem="P1")
    fleet = invoke("comms_fleet_story")
    with (EVIDENCE / "lifecycle.md").open("a") as report:
        report.write(f"\n## {stage}\n\n```json\n{json.dumps(story, indent=2)}\n```\n")
        report.write(f"\nFleet:\n```json\n{json.dumps(fleet, indent=2)}\n```\n")
    print(stage, json.dumps(story))
    return story


def setup():
    if COMMS.registry.store.path.exists():
        raise ValueError("Journey already exists; inspect it instead of replaying setup")
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(os.environ["PI_SESSION_FILE"], EVIDENCE / "worker.jsonl")
    for name in ("lead", WORKER):
        COMMS.registry.declare(COMMS.registry.new_thread(name, frozenset(), str(PROJECT),
            session_file=str(EVIDENCE / "worker.jsonl") if name == WORKER else None))
    COMMS.goals.update_goal("lead", SetGoalAction(text="Verify native stories from a real scratch order",
        goal_id="request", order=RequestGoalOrder()))
    now = time.time()
    ordered = PendingProblemOrder(id="phase5-live-order", owner=FileLine("src/agent_comms/order_story.py", 1),
        check=OrderCheck(str(PROJECT), f"test -s '{EVIDENCE / 'lifecycle.md'}'"),
        edits=("Read src/agent_comms/order_story.py:1 and describe the retained story output",),
        banned=("Do not change the original session or replay any saved input",),
        report="Report the recorded order story and the remaining delivery gaps",
        at=RecordedEventTime(now), first_call_deadline=RecordedEventTime(now + 300),
        run=MechanicalOrderRun())
    action = CreateProblemGoalAction(problem="P1", order=ordered, request="request",
        deadline=RecordedEventTime(now + 3600), commander="lead",
        repo="OpenHCSDev/agent-comms", text="Read the native story owner")
    invoke("comms_todo_create", owner=WORKER, order=FieldCodec.encode(action))
    show("Ordered")


def finish():
    from agent_comms.tool_caller import ThreadCaller
    from agent_comms.tools import CommsOrderWorkTool
    from agent_comms.todos import AcceptedVerdict, OperationDelivery, SendBackVerdict, StrikeReason

    shutil.copyfile(os.environ["PI_SESSION_FILE"], EVIDENCE / "worker.jsonl")
    work = CommsOrderWorkTool(problem="P1").apply(COMMS, ThreadCaller("lead"))
    (EVIDENCE / "work.json").write_text(json.dumps(FieldCodec.encode(work), indent=2))
    read = next(call for call in work.calls if "order_story.py" in call.digest)
    show("Worked: actual saved native read")
    todo = COMMS.goals.todos.get("P1")
    actor = COMMS.registry.require("lead").incarnation
    accepted = AcceptedVerdict(todo.revision, actor, todo.held(), read.tool_call_id,
                               "describe the retained story output")
    invoke("comms_verdict", id="P1", operation=FieldCodec.encode(accepted))
    show("Judged: accepted")
    after_acceptance()


def after_acceptance():
    from agent_comms.tool_caller import ThreadCaller
    from agent_comms.tools import CommsOrderWorkTool
    from agent_comms.todos import OperationDelivery, SendBackVerdict, StrikeReason
    work = CommsOrderWorkTool(problem="P1").apply(COMMS, ThreadCaller("lead"))
    read = next(call for call in work.calls if "order_story.py" in call.digest)
    actor = COMMS.registry.require("lead").incarnation
    todo = COMMS.goals.todos.get("P1")
    back = SendBackVerdict(todo.revision, actor, todo.held(), read.tool_call_id, StrikeReason(
        "The unit read the file but did not show the channel receiving the send back exactly once"))
    invoke("comms_verdict", id="P1", operation=FieldCodec.encode(back))
    show("Judged: sent back and struck")
    invoke("comms_order_done", problem="P1", order_id=todo.order.id,
           delivery=FieldCodec.encode(OperationDelivery()),
           claims=[{"kind": "working", "screenshot": str(EVIDENCE / "lifecycle.md")}])
    show("Delivered: real operation check passed")
    before = (ROOT / "todos.sqlite3").stat().st_mtime_ns
    assert show("Read again") == show("Read once more")
    assert (ROOT / "todos.sqlite3").stat().st_mtime_ns == before
    print("Read-only story re-read preserved the Todo store")


if __name__ == "__main__":
    if sys.argv[1] == "setup":
        setup()
    elif sys.argv[1] == "finish":
        finish()
    elif sys.argv[1] == "after-acceptance":
        after_acceptance()
