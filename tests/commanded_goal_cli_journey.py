"""Actual isolated CLI/tool order journey; never sends provider input or installs code."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main(root: Path) -> None:
    for name in tuple(os.environ):
        if name.startswith("AGENT_COMMS_") or name in (
            "PI_SESSION_ID", "PI_SESSION_FILE", "PI_PARENT_ID", "PI_TASK", "PI_WORKTREE"
        ):
            os.environ.pop(name)
    os.environ["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    from agent_comms.comms import wire
    from agent_comms.goals import IssuedGoalCommand
    from agent_comms.thread_identity import ThreadRole
    from agent_comms.threads import Thread

    root.mkdir(mode=0o700)  # Existing runs, including failed ones, are never reused.
    comms = wire(root)
    for name, role in (("commander", ThreadRole.USER), ("other", ThreadRole.USER),
                       ("unit", ThreadRole.AGENT), ("peer", ThreadRole.AGENT),
                       ("independent", ThreadRole.AGENT)):
        comms.registry.declare(Thread(name, frozenset(), str(root), role=role))
    results = []

    def cli(*arguments: str, caller: str = "", fails: str = "") -> dict:
        env = dict(os.environ)
        if caller:
            env["AGENT_COMMS_THREAD"] = caller
        command = [sys.executable, "-m", "agent_comms.cli", "--root", str(root), *arguments]
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=20)
        value = json.loads(result.stdout)
        results.append(dict(arguments=arguments, returncode=result.returncode, result=value))
        if fails:
            assert result.returncode == 1 and fails in result.stdout, (result.stdout, result.stderr)
        else:
            assert result.returncode == 0, (result.stdout, result.stderr)
        return value

    def tool(name: str, arguments: dict, caller: str, fails: str = "") -> dict:
        return cli("invoke", "--tool", name, "--arguments", json.dumps(arguments),
                   caller=caller, fails=fails)

    deadline = {"kind": "deadline", "at": time.time() + 3600}
    first = cli("order", "--from", "commander", "--to", "unit", "--text", "Deliver with @peer",
                "--deadline", json.dumps(deadline))["goal"]
    goal = comms.registry.require("unit").goal
    assert isinstance(goal.command, IssuedGoalCommand)
    original_commander = comms.registry.require("commander").incarnation
    assert goal.command.commander == original_commander
    assert goal.require_mention_source().incarnation == comms.registry.require("unit").incarnation
    assert goal.command.revision == 1 and goal.command.issued_at > 0
    assert goal.require_mention_source().bindings[0].peer == comms.registry.require("peer").incarnation

    tool("comms_order", dict(target="unit", text="Finish with @peer", deadline=deadline), "commander")
    baseline = comms.registry.require("unit").goal
    assert baseline.id == first["id"] and baseline.command.revision == 2
    report = dict(stupid="Free text hid delivery", deleted="Competing progress formatting",
                  delivery={"kind": "not_merged"}, next="Need the build owner")
    for missing in report:
        tool("comms_goal", dict(goal_id=baseline.id, status="active",
             progress={key: value for key, value in report.items() if key != missing}),
             "unit", fails=missing)
        assert comms.registry.require("unit").goal == baseline
    tool("comms_goal", dict(goal_id=baseline.id, status="active", progress="looks fine"),
         "unit", fails="complete report")
    tool("comms_goal", dict(goal_id=baseline.id, status="active",
         progress={**report, "recorded_at": time.time() + 100000}), "unit", fails="recorded_at")
    assert comms.registry.require("unit").goal == baseline

    before_report = time.time()
    tool("comms_goal", dict(goal_id=baseline.id, status="blocked", progress=report), "unit")
    blocked = comms.registry.require("unit").goal
    assert before_report <= blocked.progress.value.recorded_at <= time.time()
    assert blocked.command.revision == 2 and blocked.revision == baseline.revision + 1
    attempts = comms.goals.open_goal_store().snapshot(blocked.id)
    tool("comms_order", dict(target="unit", text="Resolve with @peer", deadline=deadline), "commander")
    revised = comms.registry.require("unit").goal
    assert revised.id == blocked.id and revised.state == blocked.state
    assert revised.command.revision == 3 and revised.revision == blocked.revision + 1
    assert revised.progress == blocked.progress
    assert not revised.progress.reported_since(revised.command.issued_at)
    assert comms.goals.open_goal_store().snapshot(blocked.id) == attempts

    cli("order", "--from", "other", "--to", "unit", "--text", "Take over",
        "--deadline", json.dumps(deadline), fails="original commander")
    tool("comms_set_goal", dict(text="Erase the order"), "unit", fails="cannot be cleared or replaced")
    tool("comms_edit_goal", dict(goal_id=blocked.id, text="Erase author binding"),
         "unit", fails="original commander")
    assert comms.registry.require("unit").goal == revised

    comms.registry.rename("commander", "renamed")
    cli("order", "--from", "renamed", "--to", "unit", "--text", "Still with @peer",
        "--deadline", '{"kind":"no_deadline"}')
    reopened = wire(root).registry.require("unit").goal
    assert reopened.command.commander == original_commander and reopened.command.revision == 4
    assert reopened.state == blocked.state and reopened.progress == blocked.progress
    assert comms.goals.open_goal_store().snapshot(blocked.id) == attempts
    tool("comms_set_goal", dict(text="Independent work"), "independent")
    independent = comms.registry.require("independent").goal
    tool("comms_goal", dict(goal_id=independent.id, status="active", progress="Plain text is valid here"),
         "independent")
    assert comms.registry.require("independent").goal.progress.summary == "Plain text is valid here"
    print(json.dumps(dict(root=str(root), checks=results, blocked_preserved=True,
                         private_attempt_unchanged=True, report_time="server recorded",
                         commander_rename_preserved=True), indent=2))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
