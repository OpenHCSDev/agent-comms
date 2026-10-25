"""Time every advertised MCP tool over curl on independent copies of a scratch bus.

Uses real Core, OAuth grants, connector processes and HTTP. Process-control calls
name a deliberately absent scratch owner: no agent is launched or signalled.
Retains request/response/timing/trace files under the supplied owned output path.
"""

from __future__ import annotations


from agent_comms.repo import Checkout
from agent_comms.repository_name import RepositoryName

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from agent_comms.change_report import FileLine
from agent_comms.channels import AnyOfMatch, ViewPredicate, ParticipantsView
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms, wire
from agent_comms.connector_caller import CALLER
from agent_comms.connector_store import ConnectorStore
from agent_comms.field_codec import FieldCodec, TurnId, GoalId, OrderId
from agent_comms.goal_actions import CreateProblemGoalAction, PausedGoalAction, SetGoalAction
from agent_comms.goals import CountCheckResult, OrderCheck
from agent_comms.owner_requests import NewestOpenRequest
from agent_comms.progress_claims import WorkingClaim
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.todos import OperationDelivery, PendingProblemOrder
from agent_comms.order_runs import MechanicalOrderRun
from agent_comms.tool_caller import ThreadCaller
from agent_comms.order_tools import CommsOrderDoneTool, CommsTodoCreateTool
from connector_readiness import ConnectorReadiness
from order_scope_fixture import FIX_SCOPE
from agent_comms.threads import Thread


PROBE_ORDER_ID = OrderId.mint()


def seed(root: Path, project: Path) -> None:
    probe_goal_id = GoalId.mint()
    root.mkdir(mode=0o700)
    comms = wire(root)
    for name in ("claude-ai", "worker", "archivable"):
        comms.registry.declare(
            Thread(name, frozenset({"probe"}), Checkout(project)),
            status=StoppedThreadStatus(),
        )
    comms.channels.set_saved_view(
        ParticipantsView('probe-view', ViewPredicate(AnyOfMatch, frozenset({'probe'})))
    )
    comms.goals.update_goal(
        "claude-ai", SetGoalAction(text="Measure the connector calls.", goal_id=probe_goal_id)
    )
    comms.goals.todos.add_request(comms.registry.require('claude-ai').incarnation,
                                 'Measure the connector calls.')
    original = comms.messaging.send("worker", "claude-ai", "Original scratch history.").message
    assert original.sender == "worker"
    for worker in threading.enumerate():
        if worker.name.startswith("agent-comms-"):
            worker.join(20)
            assert not worker.is_alive(), worker.name


def arguments(comms: Comms, name: str, project: Path, checkout: Path) -> dict:
    goal_id = comms.registry.require("claude-ai").goal.require().id
    actor = FieldCodec.encode(comms.registry.require("claude-ai").incarnation)
    pending = PendingProblemOrder(
        id=PROBE_ORDER_ID, scope=FIX_SCOPE,
        owner=FileLine(__file__, 1),
        run=MechanicalOrderRun(),
        check=OrderCheck(project, "printf 1", CountCheckResult(1)),
        edits=("Read the original saved source before changing its owner",),
        banned=("No second copy",),
        report="Report what worked",
        at=RecordedEventTime(time.time()),
        first_call_deadline=RecordedEventTime(time.time() + 300),
    )
    order = CreateProblemGoalAction(
        problem="P1",
        order=pending,
        request=NewestOpenRequest().for_order(comms.goals.todos,
                                              comms.registry.require('claude-ai').incarnation),
        review_at=RecordedEventTime(time.time() + 600),
        commander="claude-ai", assigner=comms.registry.require('claude-ai').incarnation,
        repo=RepositoryName.from_wire("OpenHCSDev/agent-comms"),
        text="Measure the connector order.",
    )
    order_tools = {
        "comms_todo",
        "comms_verdict",
        "comms_verdicts",
        "comms_fire",
        "comms_correct_wording",
        "comms_todo_list",
        "comms_order",
        "comms_todo_create",
        "comms_order_done",
        "comms_order_check",
        "comms_order_run",
        "comms_order_serve",
        "comms_problem_close",
        "comms_order_work",
        "comms_unjudged",
    }
    if name in order_tools - {'comms_todo_create'}:
        CommsTodoCreateTool(owner="worker", order=order).apply(comms, ThreadCaller("claude-ai"))
    if name == "comms_resume_goal":
        comms.goals.update_goal(
            "claude-ai", PausedGoalAction(progress="Paused only on this scratch bus.")
        )
    todo = comms.goals.todos.get("P1") if name in order_tools - {"comms_todo_create"} else None
    previous = FieldCodec.encode(todo.held()) if todo else {}
    verdict = {
        "kind": "accepted_verdict",
        "expected_revision": todo.revision if todo else 1,
        "actor": actor,
        "previous": previous,
        "tool_call_id": "absent-original-call",
        "quote": "Read the original saved source",
    }
    screenshot = project / "probe.png"
    screenshot.write_bytes(b"scratch verification artifact")
    if name == "comms_problem_close":
        CommsOrderDoneTool(
            problem="P1",
            order_id=PROBE_ORDER_ID,
            delivery=OperationDelivery(),
            claims=(WorkingClaim(str(screenshot)),),
        ).apply(comms, ThreadCaller("claude-ai"))
    if name in {"comms_decision", "comms_constraint", "comms_subtask"}:
        thread = comms.registry.require("claude-ai")
        comms.registry.declare(
            replace(thread, process_identity=ProcessIdentity.capture(os.getpid()))
        )
        owner, claim = comms.registry.live_owner_with_claim("claude-ai")
        comms.registry.lease_live_turn_with_claim(
            owner, TurnId('5e1f1a1948276272eb5c186e77e8285b'), expected_generation=claim
        )
    cases = {
        "comms_pin_channel": {"name": "#probe", "pinned": True},
        "comms_pin_thread": {"channel": "#probe", "name": "worker", "pinned": True},
        "comms_model": {"thread": "worker", "model": ""},
        "comms_sort_channels": {"order": "name"},
        "comms_tags": {},
        "comms_thread_tags": {"add": "probe"},
        "comms_channels": {},
        "comms_set_channel_metadata": {"name": "#probe", "parent": "", "archived": False},
        "comms_set_view": {
            "name": "new-view",
            "kind": "participants",
            "match": "any_of",
            "tags": "probe",
        },
        "comms_delete_view": {"name": "probe-view"},
        "comms_sort_channel": {"name": "#probe", "order": "last_activity"},
        "comms_set_project": {"path": str(project)},
        "comms_set_goal": {"text": "Measure this scratch connector."},
        "comms_goal": {
            "goal_id": str(goal_id),
            "status": "active",
            "progress": "Reading scratch history.",
        },
        "comms_resume_goal": {
            "goal_id": str(goal_id),
            "progress": "Continue the authorized scratch work.",
        },
        "comms_edit_goal": {"goal_id": str(goal_id), "text": "Measure only the scratch connector."},
        "comms_goal_history": {},
        "comms_todo": {
            "id": "P1",
            "operation": {
                "kind": "change_todo_state",
                "expected_revision": todo.revision if todo else 1,
                "state": "blocked",
                "actor": actor,
                "generation": todo.held().generation if todo else None,
            },
        },
        "comms_verdict": {"id": "P1", "operation": verdict},
        "comms_verdicts": {"verdicts": [{"problem": "P1", "operation": verdict}]},
        "comms_unjudged": {},
        "comms_fire": {
            "unit": "worker",
            "reason": (
                "The scratch unit changed the saved file without reading "
                "its original contents first."
            ),
        },
        "comms_correct_wording": {
            "unit": "worker",
            "message_id": "probe",
            "text": "Repair the authority.",
        },
        "comms_todo_list": {},
        "comms_standing_rules": {},
        "comms_order": {
            "owner": "worker",
            "order": {
                **FieldCodec.encode(order),
                "kind": "order",
                "expected_revision": todo.revision if todo else 1,
            },
        },
        "comms_todo_create": {"owner": "worker", "order": FieldCodec.encode(order)},
        "comms_order_done": {
            "problem": "P1",
            "order_id": str(PROBE_ORDER_ID),
            "delivery": {"kind": "operation"},
            "claims": [{"kind": "working", "screenshot": str(screenshot)}],
        },
        "comms_order_check": {
            "problem": "P1",
            "check": FieldCodec.encode(pending.check),
            "why": "The earlier check counted the wrong output.",
        },
        "comms_order_run": {
            "problem": "P1",
            "run": "The scratch application shows the original recorded answer.",
            "why": "The earlier run named a view the scratch app deleted.",
        },
        "comms_order_serve": {"problem": "P1", "serves": "skill"},
        "comms_problem_close": {
            "problem": "P1",
            "works": "The scratch connector returned its original recorded answer.",
            "artifact": str(screenshot),
        },
        "comms_turns": {"thread": "worker"},
        "comms_input_delivery": {"thread": "worker"},
        "comms_order_work": {"problem": "P1"},
        "comms_threads": {},
        "comms_work_branches": {"checkout": str(checkout)},
        "comms_collaboration": {
            "action": "add",
            "peer": "worker",
            "note": "Scratch connector verification.",
        },
        "comms_collaborations": {},
        "comms_rename_self": {"new_name": "claude-ai"},
        "comms_send_refusals": {},
        "comms_send": {
            "to": "worker",
            "body": "Read the saved source and report what worked.",
        },
        "comms_decision": {
            "to": "worker",
            "chosen": "Read the saved file",
            "rejected": ["Discard the saved file"],
        },
        "comms_constraint": {"to": "worker", "text": "Preserve the original saved file."},
        "comms_subtask": {"to": "worker", "text": "Read the saved file.", "completed": False},
        "comms_history": {"peer": "worker"},
        "comms_inbox": {"thread": "claude-ai", "ack": False},
        "comms_fork": {"name": "never-launched", "parent": "absent-owner"},
        "comms_stop": {"name": "absent-owner"},
        "comms_start": {"name": "absent-owner"},
        "comms_queue_restart": {"name": "absent-owner"},
        "comms_restart_queue": {"name": "worker"},
        "comms_cancel_restart": {"name": "worker"},
        "comms_archive": {"name": "archivable"},
    }
    assert set(cases) == set(CALLER.exposed()), (
        "The timing cases must cover the live declaration family."
    )
    selected = cases[name]
    if name == "comms_order":
        selected["order"].pop("repo")
        selected["order"].pop("text")
    CALLER.exposed()[name].parse_arguments(selected, call_id="timing-probe")
    return selected


def run(output: Path, checkout: Path, selected: list[str]) -> None:
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    project = output / "project"
    project.mkdir()
    original = output / "original-wire"
    seed(original, project)
    config = json.loads((Path.home() / ".config/agent-comms/commander.json").read_text())
    rows = []
    names = selected or list(CALLER.exposed())
    for name in names:
        case = output / name
        case.mkdir()
        root, home = case / "wire", case / "home"
        shutil.copytree(original, root)
        comms = wire(root)
        args = arguments(comms, name, project, checkout)
        for worker in threading.enumerate():
            if worker.name.startswith("agent-comms-"):
                worker.join(20)
                assert not worker.is_alive(), worker.name
        config_path = home / ".config/agent-comms/commander.json"
        config_path.parent.mkdir(parents=True)
        forge_auth = Path.home() / ".config/gh"
        if forge_auth.is_dir():
            (home / ".config/gh").symlink_to(forge_auth, target_is_directory=True)
        config_path.write_text(
            json.dumps(
                {
                    **config,
                    "commander": "claude-ai",
                    "root": str(root),
                    "repos": [
                        {
                            "checkout": str(checkout),
                            "github": "OpenHCSDev/agent-comms",
                            "source": "src",
                        }
                    ],
                    "state": str(case / "commander"),
                    "scratch": str(case),
                }
            )
        )
        store_path = home / ".local/state/agent-comms/connector"
        store_path.mkdir(mode=0o700, parents=True)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        readiness = ConnectorReadiness()
        environment = readiness.environment({**os.environ, "HOME": str(home), "PYTHONUNBUFFERED": "1"})
        command = (
            "import faulthandler,signal;faulthandler.register(signal.SIGUSR1,all_threads=True);"
            "from agent_comms.connector import main;raise SystemExit(main())"
        )
        with (case / "server.log").open("w") as errors:
            service = subprocess.Popen(
                [
                    sys.executable,
                    "-X",
                    "faulthandler",
                    "-u",
                    "-c",
                    command,
                    "serve",
                    "--root",
                    str(root),
                    "--port",
                    str(port),
                ],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=errors,
                text=True,
            )
            try:
                readiness.wait(service, port)
                with ConnectorStore.at(store_path).granted("scratch timing") as token:

                    def call(payload, case=case, port=port, service=service):
                        request = case / (payload["method"].replace("/", "-") + "-request.json")
                        request.write_text(json.dumps(payload))
                        curl = subprocess.Popen(
                            [
                                "curl",
                                "--silent",
                                "--show-error",
                                "-w",
                                "\n%{http_code} %{time_total}\n",
                                "-H",
                                "Content-Type: application/json",
                                "-H",
                                f"Authorization: Bearer {token}",
                                "--data-binary",
                                f"@{request}",
                                f"http://127.0.0.1:{port}/mcp",
                            ],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                        )
                        try:
                            out, err = curl.communicate(timeout=2)
                        except subprocess.TimeoutExpired:
                            service.send_signal(signal.SIGUSR1)
                            out, err = curl.communicate(timeout=180)
                        assert curl.returncode == 0, err
                        response, timing = out.strip().rsplit("\n", 1)
                        status, seconds = timing.split()
                        (case / (payload["method"].replace("/", "-") + "-response.txt")).write_text(
                            response
                        )
                        return status, float(seconds), response

                    _, _, listed = call({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                    assert name in {tool["name"] for tool in json.loads(listed)["result"]["tools"]}
                    status, seconds, response = call(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {"name": name, "arguments": args},
                        }
                    )
                    row = {
                        "tool": name,
                        "seconds": seconds,
                        "http": status,
                        "isError": json.loads(response)["result"]["isError"]
                        if status == "200"
                        else None,
                    }
                    rows.append(row)
                    (output / "timings.json").write_text(json.dumps(rows, indent=2) + "\n")
                    print(json.dumps(row), flush=True)
                    if name == "comms_send" and status == "200" and not row["isError"]:
                        saved = comms.bus.bus.message_history()
                        assert any(message.body == args["body"] for message in saved)
            finally:
                service.terminate()
                service.communicate(timeout=15)
                readiness.close()
    assert len(rows) == len(names)
    (output / "completed.json").write_text(
        json.dumps({"tools": len(rows), "completed": True}) + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--checkout", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--tools", nargs="*", choices=tuple(CALLER.exposed()), default=[])
    args = parser.parse_args()
    run(args.output.resolve(), args.checkout.resolve(), args.tools)
