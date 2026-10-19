"""Run real rotation/native dispatch on a new scratch bus, or check its reply.

No fake Comms, provider, event, transcript or history is used. A failed attempt is
retained, never replayed. This starts only the application unit being verified.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from uuid import uuid4

from agent_comms.comms import Comms
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import CommsHistoryTool


def reply(comms: Comms, commander: str, unit: str, token: str):
    history = CommsHistoryTool(peer=unit, limit=20).apply(comms, ThreadCaller(commander))
    return next((message for message in history.messages
                 if message.sender == unit and token in message.body), None)


def check(args) -> int:
    found = reply(Comms(args.root), args.commander, args.unit, args.token)
    print(int(found is not None))
    return 0


def run(args) -> int:
    from agent_comms.change_report import FileLine
    from agent_comms.commander.config import CommanderConfig, Repo
    from agent_comms.field_codec import FieldCodec
    from agent_comms.goal_actions import SetGoalAction
    from agent_comms.goals import CountCheckResult, OrderCheck, RequestGoalOrder
    from agent_comms.plan_phases import RecordPlanePhase
    from agent_comms.progress_claims import WorkingClaim
    from agent_comms.recorded_time import RecordedEventTime
    from agent_comms.thread_execution import ExternalThreadExecution
    from agent_comms.thread_status import StoppedThreadStatus
    from agent_comms.todos import MechanicalOrderRun, OperationDelivery, PendingProblemOrder
    from agent_comms.tools import CommsOrderDoneTool
    from agent_comms.work_dispatch import CheckedPhaseTask

    root = args.root.expanduser().resolve()
    allowed = Path.home() / ".cache/agent-scratch/pi-cn-phase7"
    if not root.is_relative_to(allowed) or root.exists():
        raise ValueError("Proof requires a NEW root below ~/.cache/agent-scratch/pi-cn-phase7")
    root.parent.mkdir(parents=True, exist_ok=True)
    home = root.parent / "home"
    home.mkdir()
    # Configure this scratch command installation before importing rotation's
    # existing commander context. Never rewrite the machine's live config.
    os.environ["HOME"] = str(home)
    project = Path(__file__).resolve().parents[1]
    comms = Comms(root)
    comms.bus.publisher.initialize_private_protocol()
    commander = comms.registry.declare(comms.registry.new_thread(
        "dispatch-proof-commander", frozenset(), str(project),
        model="anthropic/claude-sonnet-4-5", execution=ExternalThreadExecution,
    ), StoppedThreadStatus())
    comms.goals.update_goal(commander.name, SetGoalAction(
        text="Verify one rotation-started native unit on this scratch bus only.",
        goal_id=uuid4().hex, order=RequestGoalOrder(),
    ))
    config = CommanderConfig(
        commander=commander.name, root=root,
        cli=Path(sys.executable).with_name("agent-comms"),
        state=root.parent / "state", board=root.parent / "board.md",
        narrative=root.parent / "narrative.md", skill=project / "AGENTS.md",
        rules=(), repos=(), worktrees=comms.registry.storage_rule.checkout_base, scratch=root.parent,
        disk=root.parent,
    )
    config.write(home)
    problem = f"P{time.time_ns()}"
    unit = f"unit-{problem.lower()}-r0"
    token = f"ROTATION_REPLY_{uuid4().hex}"
    check_command = shlex.join((
        sys.executable, str(Path(__file__).resolve()), "check", "--root", str(root),
        "--commander", commander.name, "--unit", unit, "--token", token,
    ))
    order = PendingProblemOrder(
        id=uuid4().hex, owner=FileLine(__file__, 1),
        check=OrderCheck(str(project), check_command, CountCheckResult(1)),
        edits=(f"Use comms_send to send exactly {token} to {commander.name}, then return. "
               "This operation verifies your first real native reply; make no source edits.",),
        banned=("No helper agents, live-bus writes, source edits or extra tool calls.",),
        report="Send the requested token once and return; the proof commander checks delivery.",
        at=RecordedEventTime(1), first_call_deadline=RecordedEventTime(121),
        run=MechanicalOrderRun(),
    )
    RecordPlanePhase.task = CheckedPhaseTask(
        problem, Repo.of(project).github,
        "Verify rotation starts a native unit and receives its first real reply.",
        order, deadline_seconds=300,
    )
    from agent_comms.commander import rotate

    print(f"ROOT {root}", flush=True)
    print(f"SOURCE {subprocess.check_output(('git', '-C', str(project), 'rev-parse', 'HEAD'), text=True).strip()}", flush=True)
    for line in rotate.broken_rules():
        print(line, flush=True)
    assigned = comms.goals.todos.get(problem)
    started = comms.registry.require(assigned.held().owner)
    print(f"UNIT {started.name} model={started.model} pid={started.require_process().pid}", flush=True)
    print(f"ORDER {assigned.order.id} problem={problem} checkout={started.worktree}", flush=True)
    (root.parent / "order.json").write_text(json.dumps(FieldCodec.encode(assigned), indent=2) + "\n")
    before = comms.registry.all_threads()
    for line in rotate.broken_rules():
        print(line, flush=True)
    assert comms.registry.all_threads().keys() == before.keys(), "second rotation duplicated a unit"
    assert comms.goals.todos.get(problem).order.id == assigned.order.id, "second rotation duplicated an order"
    print("SECOND_ROTATION no duplicate unit or order", flush=True)

    end = time.monotonic() + 180
    found = None
    while time.monotonic() < end:
        found = reply(comms, commander.name, started.name, token)
        if found is not None:
            break
        if not comms.registry.require(started.name).process_alive:
            raise RuntimeError(f"Proof unit exited before replying; inspect {root}")
        time.sleep(0.5)
    if found is None:
        raise TimeoutError(f"No first reply; preserve {root} and inspect its original attempt")
    print(f"FIRST_REPLY {found.sender}: {found.body}", flush=True)
    artifact = root.parent / "reply.json"
    artifact.write_text(json.dumps(FieldCodec.encode(found), indent=2) + "\n")
    CommsOrderDoneTool(
        problem=problem, order_id=assigned.order.id,
        delivery=OperationDelivery(), claims=(WorkingClaim(str(artifact)),),
    ).apply(comms, ThreadCaller(commander.name))
    while time.monotonic() < end:
        current = comms.registry.require(started.name)
        if not current.turn_record.read().executing:
            break
        time.sleep(0.5)
    current.turn_record.read().require_idle()
    comms.owners.stop(started.name)
    print("STOPPED proof unit after its turn ended; scratch history retained", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(required=True)
    launch = commands.add_parser("run")
    launch.add_argument("--root", type=Path, required=True)
    launch.set_defaults(action=run)
    verify = commands.add_parser("check")
    verify.add_argument("--root", type=Path, required=True)
    verify.add_argument("--commander", required=True)
    verify.add_argument("--unit", required=True)
    verify.add_argument("--token", required=True)
    verify.set_defaults(action=check)
    args = parser.parse_args()
    return args.action(args)


if __name__ == "__main__":
    raise SystemExit(main())
