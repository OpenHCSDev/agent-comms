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
from agent_comms.rule_reviews import AnsweredRuleReviewEntry, DrawnRuleReviewEntry
from agent_comms.standing_rules import BridgeDown, DiskLow, HuntDue, PlanIdle, RulesDue, StandingRule
from agent_comms.storage_rules import UnitStorage
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.todos import CleanHunt, RecordHuntTodo, Todo
from agent_comms.connector import CALLER
from agent_comms.tools import CommsOrderServeTool, CommsRuleReviewTool, CommsSendTool, CommsStandingRulesTool, CommsTodoTool

from test_commander_problem_orders import create, native_judge_edits


@pytest.fixture
def standing(commander, commander_script):
    """The commander-file members reloaded against the scratch commander config."""
    from agent_comms.commander import capacity, judge
    for module in (judge, capacity):
        importlib.reload(module)
    module = importlib.reload(importlib.import_module("agent_comms.commander.standing"))
    state = CommanderConfig.installed().state
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
    native_judge_edits(commander, module.judge, [dict(id='recent', at=time.time(), path='a.py',
        hunks=[dict(old='before', new='after')])])
    assert lines(comms, module.IdleUnits) == [
        "IDLE UNITS with no order: worker -> give each the top unowned row now"]
    create(commander, "P1")
    assert lines(comms, module.IdleUnits) == []


def test_fork_now_and_unjudged_follow_the_unjudged_edits(commander, standing):
    comms, state, module = standing
    assert lines(comms, module.ForkNow) == lines(comms, module.Unjudged) == []
    create(commander, 'P1')
    native_judge_edits(commander, module.judge, [dict(id=f'e{n}', at=200., path='a.py',
        hunks=[dict(old='before', new='after')]) for n in range(70)])
    assert lines(comms, module.Unjudged) == ["UNJUDGED 70 edits -> judge or fork a judge slice"]
    [fork] = lines(comms, module.ForkNow)
    assert fork.startswith("FORK NOW: load 10 (1 open orders, 1 overdue, 70 unjudged edits) needs 1")


def test_fork_now_reads_the_load_of_the_bus_it_is_asked_about(commander, standing, tmp_path):
    """Three overdue orders on one bus need a fork there; a second bus without orders carries no load."""
    from agent_comms.tool_caller import ThreadCaller

    comms, _, _ = standing
    for problem in ("P1", "P2", "P3"):
        create(commander, problem)
    other = Comms(tmp_path / "other-bus")
    other.registry.declare(other.registry.new_thread("lead", frozenset(), str(tmp_path), created_at=1.0))

    def fork_now(bus):
        read = CommsStandingRulesTool().apply(bus, ThreadCaller("lead"))
        return [line for line in read.broken if line.startswith("FORK NOW")]

    assert [line.split(") ")[0] for line in fork_now(comms)] == [
        "FORK NOW: load 9 (3 open orders, 3 overdue, 0 unjudged edits"]
    assert fork_now(other) == []


def test_rules_due_until_every_drawn_rule_is_answered_and_fresh(commander, monkeypatch):
    comms, caller = commander

    def review(entry):
        return CommsRuleReviewTool(entry=entry).apply(comms, caller).broken

    assert lines(comms, RulesDue) == [
        "RULES DUE (no rule review recorded for lead) -> agent-comms-commander rules review"
        " and answer the drawn rules before any order"]
    review(DrawnRuleReviewEntry(("Declare each fact once", "Kinds are class families"), 1))
    with pytest.raises(ValueError, match="file:line or P-number"):
        review(AnsweredRuleReviewEntry(1, "This tick reads every open order before it sends any new one."))
    review(AnsweredRuleReviewEntry(1, "This tick moves rules.py:47 into one typed Core record that Core owns."))
    assert review(AnsweredRuleReviewEntry(2, "This tick sends no order until the review family answers it.")) == ()
    assert lines(comms, RulesDue) == []
    later = time.time() + RulesDue.max_age + 60
    monkeypatch.setattr(time, "time", lambda: later)
    assert lines(comms, RulesDue) == [
        "RULES DUE (last rule review was 21 min ago (max 20)) -> agent-comms-commander rules review"
        " and answer the drawn rules before any order"]



def test_a_claude_ai_directive_passes_the_commanders_directive_wording_and_review(commander, monkeypatch, tmp_path):
    """claude-ai's sends through the connector pass the same word checks as order send, and a fresh review."""
    comms, _ = commander
    comms.registry.declare(comms.registry.new_thread("claude-ai", frozenset(), str(tmp_path), created_at=3.0))
    ai = CALLER

    def send(body):
        return CommsSendTool(sender="claude-ai", target="worker", body=body).apply(comms, ai)

    clean = "The count copy at tools.py:829 is wrong: delete it and read the count from its owner."
    with pytest.raises(ValueError, match="no rule review recorded for claude-ai"):
        send(clean)
    CommsRuleReviewTool(entry=DrawnRuleReviewEntry(("Declare each fact once",), 1)).apply(comms, ai)
    CommsRuleReviewTool(entry=AnsweredRuleReviewEntry(
        1, "This tick moves rules.py:47 into one typed Core record that Core owns.")).apply(comms, ai)
    for body, refusal in (
        ("Slop: state; fact the count of open orders; at tools.py:829; derive delete-copy on todos.py:1\n"
         "Read the count from its owner.", "Not blunt"),
        ("The canonical count at tools.py:829 is wrong: read it from its owner.", "Jargon"),
        ("Thanks for moving the count at tools.py:829 into its owner, that is correct.", "Soft wording"),
    ):
        with pytest.raises(ValueError, match=refusal):
            send(body)
    send(clean)
    assert [(m.sender, m.body) for m in comms.bus.inbox("worker")] == [("claude-ai", clean)]
    later = time.time() + RulesDue.max_age + 60
    monkeypatch.setattr(time, "time", lambda: later)
    with pytest.raises(ValueError, match="last rule review was 21 min ago"):
        send(clean)

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
