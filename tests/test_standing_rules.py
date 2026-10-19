import importlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import replace

import pytest

from agent_comms.change_report import FileLine
from agent_comms.child_process import ProcessIdentity
from agent_comms.commander.config import CommanderConfig, Repo
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.plan_phases import RecordPlanePhase, ReviewQueuePhase, UnplannedPhase
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.rule_reviews import AnsweredRuleReviewEntry, DrawnRuleReviewEntry
from agent_comms.standing_rules import (BoardDisagrees, BridgeDown, DiskLow, HuntDue, MemoryLow, PlanIdle,
    RulesDue, StandingRule)
from agent_comms.storage_rules import MemoryHolder, UnitStorage
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.todos import (CleanHunt, DefectText, DirectiveText, MergedProgress,
    NoCommitProgress, OpenPrProgress, OrderProgress, PullRequest, PullRequests, StrikeReason, Todo, UnreadProgress)
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


@pytest.mark.parametrize("reported,alarms", [
    (NoCommitProgress(), True),
    (MergedProgress("d" * 9), False),
    (OpenPrProgress(1581, "b" * 9, at=500), False),
    (UnreadProgress("forge unavailable"), False),
])
def test_board_self_check_uses_order_progress_and_its_linked_merge(
        commander, standing, monkeypatch, reported, alarms):
    comms, caller = commander
    owned, pending, foreign = (create(commander, pid) for pid in ("P77", "P109", "P117"))
    stamp = "1970-01-01T00:08:20Z"
    merged = PullRequest.listed(1581, "docs/module-layers", "b" * 40, "d" * 40,
        stamp, stamp, owned.order.pull_request_body("Module layer map"))
    opened = PullRequest.listed(1624, "ratchet/none-absence", "c" * 40, "", "",
        stamp, pending.order.pull_request_body("Declared measure"))
    other = PullRequest.listed(1629, "ratchet/decoder-marker", "e" * 40, "f" * 40,
        stamp, stamp, "Agent-Comms-Order: another-order\n")

    def forge(self, repo, state, order):
        return (merged, other) if state == "merged" else (opened,)

    monkeypatch.setattr(PullRequests, "read", forge)
    assert BoardDisagrees.violations(comms, "lead") == ()
    reads = []

    def broken_board(cls, todo, storage, pulls):
        reads.append(todo.id)
        return reported

    monkeypatch.setattr(OrderProgress, "read", classmethod(broken_board))
    expected = [f"BOARD DISAGREES: P77 order {owned.order.id} reads no commit, "
                "but its linked PR merged as ddddddddd -> repair the progress read this wake"] if alarms else []
    assert lines(comms, BoardDisagrees) == expected
    assert set(reads) == {owned.id, pending.id, foreign.id}
    # Rotation calls this tool each wake. A still-broken board must survive its after cursor.
    read = CommsStandingRulesTool(after=time.time() + 100).apply(comms, caller)
    assert [line for line in read.broken if line.startswith("BOARD DISAGREES:")] == expected


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
    comms.goals.todos.record_hunt(actor, hunt)
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


def working(comms, name, created):
    """A unit whose native session records a turn now."""
    from datetime import UTC, datetime
    stamp = datetime.now(UTC).isoformat()
    path = comms.root / f"{name}-session.jsonl"
    rows = [dict(type="session", id=f"{name}-session", version=3, timestamp=stamp),
            dict(type="message", id=f"{name}-input", timestamp=stamp,
                 message=dict(role="user", content="Repair the owner",
                              timestamp=time.time() * 1000))]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)
    if name in comms.registry.all_threads():
        comms.registry.register(replace(comms.registry.require(name), session_file=str(path)))
    else:
        comms.registry.declare(comms.registry.new_thread(
            name, frozenset(), str(comms.root), session_file=str(path), created_at=created))


def test_wheel_spin_strikes_and_reassigns_an_order_flat_across_two_readings(
        commander, standing, tmp_path, monkeypatch):
    from agent_comms.storage_rules import StorageRule
    from agent_comms.todos import MovingTrend, OrderProgress, PullRequests
    from agent_comms.tool_caller import ThreadCaller
    comms, state, module = standing
    storage = StorageRule(checkout_base=tmp_path / "wt")
    monkeypatch.setattr(comms.registry, "storage_rule", storage)
    origin, unit = tmp_path / "origin.git", storage.order_checkout("worker", "P1")
    unit.mkdir(parents=True)

    def git(*args):
        return subprocess.run(["git", "-C", str(unit), "-c", "user.name=t", "-c", "user.email=t@t",
                               *args],
                              check=True, capture_output=True, text=True).stdout

    def commit(left):
        (unit / "left").write_text(f"{left}\n")
        (unit / "work").write_text(f"{time.time()}\n")
        git("add", "-A")
        git("commit", "-q", "-m", f"{left} left")

    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    git("init", "-q", "-b", "main")
    git("remote", "add", "origin", str(origin))
    commit(3)
    git("push", "-q", "origin", "main")
    git("fetch", "-q", "origin")
    git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    git("switch", "-q", "-c", "fix/p1")
    runs = tmp_path / "runs"
    check = f"echo run >> {runs}; cat left"
    todo = create(commander, "P1", command=check, expected=0, deadline=time.time() + 3600)

    def progress():
        return OrderProgress.read(comms.goals.todos.get("P1"), storage, PullRequests())

    commit(2)
    assert "; distance 2, projected done " in progress().line
    assert isinstance(progress().distance_readings().trend, MovingTrend)
    commit(2)
    assert progress().line.endswith("; distance 2, not falling")
    assert runs.read_text().count("run") == 3  # once per commit: base, then each branch commit

    reader = ("import sys; from agent_comms.goals import CountCheckResult, OrderCheck; "
              "check = OrderCheck(sys.argv[1], sys.argv[2], CountCheckResult(0)); "
              "print(check.at(sys.argv[3], sys.argv[1]).stdout.decode(), end='')")
    second = subprocess.run([sys.executable, "-c", reader, str(unit), check,
                             git("rev-parse", "HEAD").strip()],
                            check=True, capture_output=True, text=True)
    assert second.stdout == "2\n" and runs.read_text().count("run") == 3
    working(comms, "worker", 2.0)
    working(comms, "spare", 9.0)

    read = CommsStandingRulesTool().apply(comms, ThreadCaller("lead"))
    assert ("WHEEL SPIN: worker kept working on P1 while its distance to done did not fall across"
            " two readings -> Core strikes it and gives the order to an idle unit") in read.broken
    moved = comms.goals.todos.get("P1")
    assert [strike.previous.owner for strike in moved.live_strikes()] == ["worker"]
    assert moved.holder == "spare" and moved.deadline == todo.deadline
    assert moved.order.id != todo.order.id and moved.order.check == todo.order.check

    create(commander, "P2", owner="worker", deadline=time.time() + 3600)
    taken = StrikeReason("The unit was taken off P2 so the test leaves it unowned.")
    comms.goals.fire("worker", taken, "lead")
    assert comms.goals.todos.get("P2").holder is None
    read = CommsStandingRulesTool().apply(comms, ThreadCaller("lead"))
    assert ("WHEEL SPIN: worker holds no order while P2 is unowned -> Core gives it to worker"
            in read.broken)
    assert comms.goals.todos.get("P2").holder == "worker"


def test_unjudged_follows_the_unjudged_edits(commander, standing):
    comms, state, module = standing
    assert lines(comms, module.Unjudged) == []
    create(commander, 'P1')
    native_judge_edits(commander, module.judge, [dict(id=f'e{n}', at=200., path='a.py',
        hunks=[dict(old='before', new='after')]) for n in range(70)])
    assert lines(comms, module.Unjudged) == ["UNJUDGED 70 edits -> judge or fork a judge slice"]


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



def test_defect_text_without_slop_line_needs_no_label():
    """Only a text that names a defect in a Slop: line needs its label outside that line."""
    assert DefectText.found("Yes, go ahead.") == [] and DirectiveText.refused("Yes, go ahead.") == ()
    slop = "Slop: IDEN-5; fact the count of open orders; at tools.py:829; derive delete-copy on todos.py:1"
    assert DefectText.found(slop + "\nRead the count from its owner.") == [slop]
    assert DirectiveText.refused(slop + "\nRead the count from its owner.") == (DefectText.refusal,)
    assert DefectText.found(slop + "\nThe copy at tools.py:829 is wrong.") == []


def test_a_claude_ai_directive_passes_the_commanders_wording_with_no_rules_review(commander, tmp_path):
    """claude-ai directs the commanders: its sends pass the same word checks as order send and
    need no rules review."""
    comms, _ = commander
    comms.registry.declare(comms.registry.new_thread("claude-ai", frozenset(), str(tmp_path), created_at=3.0))

    def send(body):
        return CommsSendTool(sender="claude-ai", target="worker", body=body).apply(comms, CALLER)

    for body, refusal in (
        ("Slop: IDEN-5; fact the count of open orders; at tools.py:829; derive delete-copy on todos.py:1\n"
         "Read the count from its owner.", "Not blunt"),
        ("The canonical count at tools.py:829 is wrong: read it from its owner.", "Jargon"),
        ("Thanks for moving the count at tools.py:829 into its owner, that is correct.", "Soft wording"),
    ):
        with pytest.raises(ValueError, match=refusal):
            send(body)
    clean = "The count copy at tools.py:829 is wrong: delete it and read the count from its owner."
    send(clean)
    assert [(m.sender, m.body) for m in comms.bus.inbox("worker")] == [("claude-ai", clean)]

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


def test_memory_low_fires_under_the_threshold_naming_idle_temp_dirs(commander, tmp_path):
    """Available memory under the StorageRule's threshold names the pytest temp dirs on the temp root
    idle over its minutes, largest first, and puts a cleanup owner on it; over the threshold, nothing."""
    comms, _ = commander
    idle, busy = (tmp_path / "tmp" / "pytest-of-t" / name for name in ("pytest-1", "pytest-2"))
    for path in (idle, busy):
        path.mkdir(parents=True)
        (path / "held").write_bytes(b"x" * 2**20)
    old = time.time() - 31 * 60
    os.utime(idle, (old, old))

    def broken(memory_gib):
        (comms.root / "storage.json").write_text(json.dumps(
            {"temp_root": str(tmp_path / "tmp"), "memory_gib": memory_gib}))
        return MemoryLow.violations(Comms(comms.root), "lead")

    assert broken(0) == ()
    low, = broken(10_000)
    assert low.memory.idle_dirs == (MemoryHolder(str(idle), 2**20 / 2**30),)
    assert f"temp dirs idle over 30 min: {idle} 0.0 GiB;" in low.line() and str(busy) not in low.line()
    assert low.line().startswith("MEMORY LOW: ") and "cleanup due under 10000 GiB" in low.line()
    assert low.line().endswith(" -> put a cleanup owner on it now")


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
