import importlib
import json
import os
import shutil
import subprocess
import time

import pytest

from agent_comms.change_report import FileLine
from agent_comms.child_process import ProcessIdentity
from agent_comms.commander.config import CommanderConfig, Repo
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.plan_phases import RecordPlanePhase, ReviewQueuePhase, UnplannedPhase
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.standing_rules import BridgeDown, DiskLow, HuntDue, PlanIdle, StandingRule
from agent_comms.storage_rules import UnitStorage
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.todos import CleanHunt, RecordHuntTodo, Todo
from agent_comms.tools import CommsOrderServeTool, CommsStandingRulesTool, CommsTodoTool

from test_commander_problem_orders import create


@pytest.fixture
def standing(commander, commander_script):
    """The commander-file members reloaded against the scratch commander config."""
    from agent_comms.commander import capacity, judge, rules
    for module in (judge, rules, capacity):
        importlib.reload(module)
    module = importlib.reload(importlib.import_module("agent_comms.commander.standing"))
    state = CommanderConfig.installed().state
    (state / "activity.log").write_text("")
    return commander[0], state, module


def lines(comms, member, commander="lead"):
    return [found.line() for found in member.violations(comms, commander)]


def test_plan_idle_names_a_ready_phase_until_an_order_serving_it_is_open(commander):
    comms, caller = commander

    def idle():
        return [rule.phase for rule in PlanIdle.violations(comms, "lead")]

    assert RecordPlanePhase in idle() and ReviewQueuePhase not in idle()
    assert PlanIdle(RecordPlanePhase).line() == (
        "PLAN IDLE: phase record_plane ready, no open order -> dispatch it this wake")
    create(commander, "P1")
    assert comms.goals.todos.get("P1").order.serves is UnplannedPhase
    assert RecordPlanePhase in idle()
    CommsOrderServeTool(problem="P1", serves=RecordPlanePhase).apply(comms, caller)
    stored = comms.goals.todos.get("P1")
    assert stored.order.serves is RecordPlanePhase
    assert FieldCodec.decode(Todo, FieldCodec.encode(stored)) == stored
    assert RecordPlanePhase not in idle()


def test_hunt_due_until_a_hunt_is_recorded(commander):
    comms, caller = commander
    todo = create(commander, "P1")
    assert lines(comms, HuntDue) == ["HUNT DUE: read a hot path and order the fix this wake"]
    hunt = CleanHunt(FileLine(__file__, 1), "This source reads the same rows on every event "
                     "without changing any owned fact.", RecordedEventTime(time.time()))
    actor = comms.registry.require("lead").incarnation
    CommsTodoTool(id="P1", operation=RecordHuntTodo(todo.revision, actor, hunt)).apply(comms, caller)
    assert lines(comms, HuntDue) == []


def test_bridge_down_while_the_commander_endpoint_receives_nothing(commander, tmp_path):
    comms, _ = commander
    comms.registry.declare(comms.registry.new_thread(
        "watching", frozenset(), str(tmp_path), created_at=3.0, execution=ExternalThreadExecution),
        StoppedThreadStatus())
    assert lines(comms, BridgeDown, "watching") == [
        "COMMS BRIDGE DOWN: the commander receives nothing -> re-arm the bridge Monitor this wake"]
    with comms.registry.external_process("watching", ProcessIdentity.capture(os.getpid())):
        assert lines(comms, BridgeDown, "watching") == []


def test_idle_units_lists_active_units_without_an_order(commander, standing):
    comms, state, module = standing
    (state / "activity.log").write_text("12:00:00 worker read a file\n12:00:01 lead sent\n")
    assert lines(comms, module.IdleUnits) == [
        "IDLE UNITS with no order: worker -> give each the top unowned row now"]
    create(commander, "P1")
    assert lines(comms, module.IdleUnits) == []


def test_fork_now_and_unjudged_follow_the_unjudged_edits(standing):
    comms, state, module = standing
    assert lines(comms, module.ForkNow) == lines(comms, module.Unjudged) == []
    (state / "edits.jsonl").write_text("".join(json.dumps({"id": f"e{n}", "at": 1.0, "thread": "worker", "path": "a.py", "hunks": []}) + "\n" for n in range(70)))
    assert lines(comms, module.Unjudged) == ["UNJUDGED 70 edits -> judge or fork a judge slice"]
    [fork] = lines(comms, module.ForkNow)
    assert fork.startswith("FORK NOW: load 7 (0 open orders, 0 overdue, 70 unjudged edits) needs 1")


def test_rules_due_until_a_fresh_review_is_answered(standing):
    comms, state, module = standing
    assert lines(comms, module.RulesDue) == [
        "RULES DUE (no rule review recorded for lead) -> agent-comms-commander rules review"
        " and answer the drawn rules before any order"]
    (state / "rules.jsonl").write_text(
        json.dumps({"kind": "review", "who": "lead", "at": time.time(), "drawn": []}) + "\n")
    assert lines(comms, module.RulesDue) == []


def test_the_read_tool_returns_every_members_lines(commander, standing):
    comms, caller = commander
    read = CommsStandingRulesTool().apply(comms, caller)
    assert read.broken == tuple(rule.line() for rule in StandingRule.broken(comms, "lead"))
    assert "HUNT DUE: read a hot path and order the fix this wake" in read.broken


def test_storage_rules_report_each_threshold_on_a_scratch_root(commander, tmp_path):
    comms, _ = commander
    create(commander, "P1")
    home, extra = tmp_path / "wt" / "worker", tmp_path / "wt" / "worker--P1"
    subprocess.run(["git", "init", "-q", str(home)], check=True)
    subprocess.run(["git", "-C", str(home), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                    "--allow-empty", "-m", "base"], check=True)
    subprocess.run(["git", "-C", str(home), "worktree", "add", "-q", str(extra)], check=True)
    free = shutil.disk_usage(tmp_path).free / 2**30

    def broken(member, **limits):
        (comms.root / "storage.json").write_text(json.dumps(
            {"volume": str(tmp_path), "checkout_base": str(tmp_path / "wt"), **limits}))
        return member.violations(Comms(comms.root), "lead")

    assert broken(UnitStorage, cleanup_gib=0, floor_gib=0, max_checkouts=1) == ()
    assert broken(DiskLow, cleanup_gib=0, floor_gib=0, max_checkouts=1) == ()
    over, = broken(UnitStorage, cleanup_gib=0, floor_gib=0, max_checkouts=0)
    assert over.checkouts == (home.resolve(), extra.resolve())
    assert over.line() == ("VIOLATION storage quota: worker holds 1 checkouts besides home, limit 0"
                           " -> order the cleanup")
    cleanup, = broken(DiskLow, cleanup_gib=free + 100, floor_gib=0)
    assert "cleanup due under" in cleanup.line() and "floor" not in cleanup.line()
    floor, = broken(DiskLow, cleanup_gib=free + 100, floor_gib=free + 50)
    assert floor.line().endswith("floor nothing allocates) -> put a cleanup owner on it now")


def test_merge_growth_names_each_merge_over_the_limit(commander, standing, monkeypatch, tmp_path):
    comms, _, module = standing
    origin, checkout = tmp_path / "growth.git", tmp_path / "growth"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    checkout.mkdir()

    def git(*args):
        identity = ("-c", "user.name=t", "-c", "user.email=t@t")
        command = ["git", "-C", str(checkout), *identity, *args]
        run = subprocess.run(command, check=True, capture_output=True, text=True)
        return run.stdout.strip()

    git("init", "-q", "-b", "main")
    git("remote", "add", "origin", str(origin))
    git("commit", "-q", "--allow-empty", "-m", "base")
    for branch, grown in (("big", 41), ("small", 40)):
        git("checkout", "-q", "-b", branch, "main")
        (checkout / "src").mkdir(exist_ok=True)
        (checkout / "src" / f"{branch}.py").write_text("x = 1\n" * grown)
        git("add", "-A")
        git("commit", "-q", "-m", f"{branch} work")
        git("checkout", "-q", "main")
        git("merge", "-q", "--no-ff", "-m", f"Merge {branch}", branch)
    git("push", "-q", "origin", "main")
    monkeypatch.setattr(module, "CONFIG", module.CONFIG.model_copy(update={"repos": (
        Repo(checkout=checkout, github="fleet/growth"),)}))
    big = git("rev-parse", "main~1")[:9]
    assert lines(comms, module.MergeGrowth) == [
        f"GROWTH fleet/growth {big} src +41/-0 (net +41) tests +0/-0: Merge big "
        "-> justify or send back the owner with a deletion order"]
