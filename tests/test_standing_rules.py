import importlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.change_report import FileLine
from agent_comms.child_process import ProcessIdentity
from agent_comms.commander.config import CommanderConfig
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.plan_phases import RecordPlanePhase, ReviewQueuePhase, UnplannedPhase
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.rule_reviews import AnsweredRuleReviewEntry, DrawnRuleReviewEntry
from agent_comms.standing_rules import (BoardDisagrees, BridgeDown, DiskLow, HuntDue, MemoryLow, PlanIdle,
    RulesDue, StandingRule, OrderOverdue, FirstCallMissing, OwnerIdle)
from agent_comms.storage_rules import MemoryHolder, UnitStorage
from agent_comms.thread_execution import ExternalThreadExecution, NativeThreadExecution
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.todos import (CleanHunt, DirectiveText, MergedProgress,
    MergedPullRequest, NoCommitProgress, OpenPrProgress, OrderProgress, PullRequests, StrikeReason,
    Todo, UnmergedPullRequest, UnreadProgress)
from agent_comms.connector import CALLER
from agent_comms.tools import CommsOrderServeTool, CommsRuleReviewTool, CommsSendTool, CommsStandingRulesTool, CommsChangeProblemTool

from test_commander_problem_orders import create, native_judge_edits


@pytest.fixture
def standing(commander, commander_script):
    """The commander-file members reloaded against the scratch commander config."""
    from agent_comms.commander import capacity, judge
    for module in (judge, capacity):
        importlib.reload(module)
    module = importlib.reload(importlib.import_module("agent_comms.commander.standing"))
    state = CommanderConfig.read_root(commander[0].root).state
    return commander[0], state, module


def lines(comms, member, commander="lead"):
    return [found.line() for found in member.violations(comms, commander)]


def test_wheel_spin_reads_external_owner_and_requires_actual_recent_turns(standing, tmp_path):
    from agent_comms.transcript_events import AssistantTranscript

    comms, _, rules = standing
    for name in ('recent-cli', 'old-cli', 'empty-cli'):
        comms.registry.declare(comms.registry.new_thread(
            name, frozenset(), str(tmp_path), execution=ExternalThreadExecution))
    for name, timestamp in (('recent-cli', time.time()), ('old-cli', 1.0)):
        comms.transcripts.external(name).finish(turn_id='original-turn', item_id='reply', events=(
            AssistantTranscript('retained answer', position=1, timestamp=timestamp),))
    assert [thread.name for thread in rules.WheelSpin.idle(comms, 'lead')] == ['recent-cli']


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
    merged = MergedPullRequest(1581, "docs/module-layers", "b" * 40, merge="d" * 40,
        merged_at=500, updated_at=500, orders=(owned.order.id,))
    opened = UnmergedPullRequest(1624, "ratchet/none-absence", "c" * 40,
        updated_at=500, orders=(pending.order.id,))
    other = MergedPullRequest(1629, "ratchet/decoder-marker", "e" * 40, merge="f" * 40,
        merged_at=500, updated_at=500, orders=("another-order",))

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
        "PLAN IDLE: phase record_plane ready, no open order -> "
        "no checked task declared; supply its source, check and brief")
    create(commander, "P1")
    assert comms.goals.todos.get("P1").order.serves is UnplannedPhase
    assert RecordPlanePhase in idle()
    CommsOrderServeTool(problem="P1", serves=RecordPlanePhase).apply(comms, caller)
    stored = comms.goals.todos.get("P1")
    assert stored.order.serves is RecordPlanePhase
    assert FieldCodec.decode(Todo, FieldCodec.encode(stored)) == stored
    assert RecordPlanePhase not in idle()


def test_hunt_due_until_a_hunt_is_recorded(commander, commander_script):
    comms, caller = commander
    _, repo = commander_script
    hot = repo / 'src' / 'hot.py'
    hot.parent.mkdir()
    hot.write_text('counter = 1\n')
    for command in (('add', '.'), ('-c', 'user.name=t', '-c', 'user.email=t@t',
                    'commit', '-q', '-m', 'hot source'),
                    ('update-ref', 'refs/remotes/origin/main', 'HEAD')):
        subprocess.run(['git', '-C', str(repo), *command], check=True, capture_output=True)
    todo = create(commander, "P1")
    with comms.goals.todos.writing() as db:
        replace(todo, order=replace(todo.order,
            check=replace(todo.order.check, directory=str(repo)))).upsert(db)
    assert HuntDue.next_hunt(comms, 'lead') == (str(hot),)
    assert lines(comms, HuntDue) == [f'HUNT DUE: read {hot} and order the fix this wake']
    hunt = CleanHunt(FileLine(__file__, 1), "This source reads the same rows on every event "
                     "without changing any owned fact.", RecordedEventTime(time.time()))
    actor = comms.registry.require("lead").incarnation
    comms.goals.todos.record_hunt(actor, hunt)
    assert lines(comms, HuntDue) == []


@pytest.mark.parametrize('execution', (NativeThreadExecution, ExternalThreadExecution),
                         ids=('native', 'external'))
async def test_order_alarm_family_is_delivered_once_without_a_monitor_and_resolves(
        commander, standing, tmp_path, monkeypatch, execution):
    from contextlib import nullcontext
    from datetime import UTC, datetime
    from agent_comms.commander_heartbeat import CommanderHeartbeat
    from agent_comms.coordinator import Coordination
    from agent_comms.transcript_events import ToolStartTranscript

    comms, _ = commander
    now = [9000.0]
    monkeypatch.setattr(time, 'time', lambda: now[0])
    for created, name in enumerate(('late', 'missing', 'idle'), 3):
        comms.registry.declare(comms.registry.new_thread(
            name, frozenset(), str(tmp_path), created_at=float(created), execution=execution))

    def tool_call(name, timestamp):
        identity = f'{name}-{timestamp}'
        if execution is ExternalThreadExecution:
            comms.transcripts.external(name).finish(turn_id=identity, item_id=identity, events=(
                ToolStartTranscript(position=0, tool_call_id=identity,
                                    tool_name='bash', timestamp=timestamp),))
            return
        path = tmp_path / f'{name}-native.jsonl'
        stamp = datetime.fromtimestamp(timestamp, UTC).isoformat()
        if not path.exists():
            path.write_text(json.dumps({'type': 'session', 'version': 3, 'id': name,
                                        'timestamp': stamp}) + '\n')
            path.chmod(0o600)
        with path.open('a') as stream:
            stream.write(json.dumps({
                'type': 'message', 'id': identity, 'timestamp': stamp,
                'message': {'role': 'assistant', 'content': [
                    {'type': 'toolCall', 'id': identity, 'name': 'bash',
                     'arguments': {'command': 'pwd'}}]}}) + '\n')
        comms.registry.register(replace(comms.registry.require(name), session_file=str(path)))

    tool_call('late', 8990)
    tool_call('idle', 8000)
    create(commander, 'P901', owner='late', deadline=11000)
    create(commander, 'P902', owner='missing', deadline=11000)
    create(commander, 'P903', owner='idle', deadline=11000)
    # Another order held by the idle owner must not duplicate the owner alarm.
    create(commander, 'P904', owner='idle', deadline=11000)
    families = (OrderOverdue, FirstCallMissing, OwnerIdle)

    def alarms():
        broken = StandingRule.broken(comms, 'lead')
        return {family: tuple(rule for rule in broken if type(rule) is family)
                for family in families}

    first = comms.goals.todos.get('P902')
    with comms.goals.todos.writing() as db:
        replace(first, order=replace(first.order,
            first_call_deadline=RecordedEventTime(11000))).upsert(db)
    assert all(not rules for rules in alarms().values())
    heartbeat = CommanderHeartbeat()
    lead = replace(comms.registry.require('lead'), execution=ExternalThreadExecution)
    comms.registry.register(lead)
    with comms.registry.external_process('lead', ProcessIdentity.capture(os.getpid())):
        await Coordination.run_worker(lambda: heartbeat.watch_pass(lambda: nullcontext(comms)))
        now[0] = 10000.0
        overdue = comms.goals.todos.get('P901')
        missing = comms.goals.todos.get('P902')
        with comms.goals.todos.writing() as db:
            replace(overdue, deadline=RecordedEventTime(9999)).upsert(db)
            replace(missing, order=replace(missing.order,
                first_call_deadline=RecordedEventTime(9998))).upsert(db)
        found = alarms()
        assert all(len(found[family]) == 1 for family in families)
        assert found[OrderOverdue][0].began() == 9999
        assert found[FirstCallMissing][0].began() == 9998
        assert found[OwnerIdle][0].began() == 9200
        await Coordination.run_worker(lambda: heartbeat.watch_pass(lambda: nullcontext(comms)))
        messages = comms.bus.inbox('lead')
        assert len(messages) == 1 and messages[0].sender == 'commander-heartbeat'
        assert messages[0].body.count('RULE ORDER OVERDUE P901 late') == 1
        assert messages[0].body.count('RULE FIRST CALL MISSING P902 missing') == 1
        assert messages[0].body.count('RULE OWNER IDLE idle') == 1
        await Coordination.run_worker(lambda: heartbeat.watch_pass(lambda: nullcontext(comms)))
        assert len(comms.bus.inbox('lead')) == 1
        with comms.goals.todos.writing() as db:
            replace(overdue, deadline=RecordedEventTime(11000)).upsert(db)
        tool_call('missing', 10000)
        tool_call('idle', 10000)
        assert all(not rules for rules in alarms().values())
        await Coordination.run_worker(lambda: heartbeat.watch_pass(lambda: nullcontext(comms)))
        resolved = comms.bus.inbox('lead')[-1].body
        assert 'NO LONGER CURRENT: ORDER OVERDUE P901 late' in resolved
        assert 'NO LONGER CURRENT: FIRST CALL MISSING P902 missing' in resolved
        assert 'NO LONGER CURRENT: OWNER IDLE idle' in resolved


def test_owner_idle_ignores_delivered_orders_but_reports_pending_work(commander, monkeypatch):
    from agent_comms.todos import OperationDelivery
    from agent_comms.transcript_events import ToolStartTranscript

    comms, _ = commander
    monkeypatch.setattr(time, 'time', lambda: 10000.0)
    comms.registry.register(replace(comms.registry.require('worker'),
                                    execution=ExternalThreadExecution))
    comms.transcripts.external('worker').finish(turn_id='old', item_id='call', events=(
        ToolStartTranscript(position=0, tool_call_id='call', tool_name='bash', timestamp=8000),))
    todo = create(commander, 'P1', deadline=11000)
    with comms.goals.todos.writing() as db:
        replace(todo, order=todo.order.delivered(
            OperationDelivery(at=RecordedEventTime(9900)))).upsert(db)
    held = comms.goals.todos.get('P1')
    assert held.holder == 'worker' and not held.pending
    assert OwnerIdle.violations(comms, 'lead') == ()
    create(commander, 'P2', deadline=11000)
    assert OwnerIdle.violations(comms, 'lead') == (OwnerIdle('worker', 9200),)


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


def test_idle_reads_external_execution_and_requires_an_actual_recent_turn(standing, tmp_path):
    from agent_comms.transcript_events import AssistantTranscript
    comms, _, module = standing
    for name in ("external-working", "external-empty"):
        comms.registry.declare(comms.registry.new_thread(
            name, frozenset(), str(tmp_path), execution=ExternalThreadExecution))
    comms.transcripts.external("external-working").finish(
        turn_id="current", item_id="answer", events=(AssistantTranscript("working", position="answer", timestamp=time.time()),))
    names = [thread.name for thread in module.WheelSpin.idle(comms, "lead")]
    assert "external-working" in names
    assert "external-empty" not in names and "worker" not in names


def test_wheel_spin_restarts_a_day_old_deadline_at_handoff(commander, standing):
    comms, _, module = standing
    now, window = time.time(), 900
    deadline = now - 86400
    todo = create(commander, "P1", deadline=deadline)
    original = replace(todo, order=replace(todo.order,
        at=RecordedEventTime(deadline - window),
        first_call_deadline=RecordedEventTime(deadline - window + 120)))
    with comms.goals.todos.writing() as db:
        original.upsert(db)
    assert original.overdue(now)
    working(comms, "worker", 2.0)
    working(comms, "spare", 3.0)
    comms.goals.todos.advance_objective_failures(comms.goals)
    assert comms.goals.todos.get("P1").holder is None
    module.UnownedWheelSpin.after_stall(comms)
    moved = comms.goals.todos.get("P1")
    issued = moved.order.checked().at.recorded_at("An order")
    assert moved.holder == "spare"
    assert moved.deadline == RecordedEventTime(issued + window)
    assert not moved.overdue(issued)


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
    ordered_check = todo.order.checked().check
    ordered_check.at(git("rev-parse", "HEAD").strip(), str(unit), unit.parent)

    def progress():
        return OrderProgress.read(comms.goals.todos.get("P1"), storage, PullRequests())

    commit(2)
    ordered_check.at(git("rev-parse", "HEAD").strip(), str(unit), unit.parent)
    assert "; distance 2, projected done " in progress().line
    assert isinstance(progress().distance_readings().trend, MovingTrend)
    commit(2)
    ordered_check.at(git("rev-parse", "HEAD").strip(), str(unit), unit.parent)
    assert progress().line.endswith("; distance 2, not falling")
    assert runs.read_text().count("run") == 3  # once per commit: base, then each branch commit

    reader = ("import sys; from agent_comms.goals import CountCheckResult, OrderCheck; "
              "check = OrderCheck(sys.argv[1], sys.argv[2], CountCheckResult(0)); "
              "print(check.recorded(sys.argv[3], sys.argv[1])[0].stdout.decode(), end='')")
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
    assert moved.holder == "spare"
    assert moved.deadline == todo.reissued_deadline(moved.order.checked().at.recorded_at("An order"))
    assert moved.order.id != todo.order.id and moved.order.check == todo.order.check

    create(commander, "P2", owner="worker", deadline=time.time() + 3600)
    taken = StrikeReason("The unit was taken off P2 so the test leaves it unowned.")
    comms.goals.fire("worker", taken, "lead")
    assert comms.goals.todos.get("P2").holder is None
    [unowned] = module.UnownedWheelSpin.violations(comms, "lead")
    assert unowned.todo.id == "P2"
    # Firing releases P2 without striking its recently active worker on that order.
    # Reuse that eligible idle unit instead of allocating a second process.
    assert type(unowned.dispatch) is module.ReusedWorkDispatch
    assert unowned.unit == "worker"
    assert unowned.line() == (
        f"WHEEL SPIN: {unowned.unit} holds no order while P2 is unowned"
        f" -> Core gives it to {unowned.unit}")
    # A second pass over the same board is the same violation, so the heartbeat stays quiet.
    assert frozenset(module.UnownedWheelSpin.violations(comms, "lead")) == frozenset([unowned])
    # Actual unit launch/first reply is a separate native application proof.
    assert comms.goals.todos.get("P2").holder is None


def test_unjudged_follows_the_unjudged_edits(commander, standing):
    from agent_comms.commander import judge
    comms, state, module = standing
    assert lines(comms, module.Unjudged) == []
    create(commander, 'P1')
    native_judge_edits(commander, judge, [dict(id=f'e{n}', at=200., path='a.py',
        hunks=[dict(old='before', new='after')]) for n in range(70)])
    assert lines(comms, module.Unjudged) == ["UNJUDGED 70 edits -> judge or fork a judge slice"]



def test_rules_due_until_every_drawn_rule_is_answered_and_fresh(commander, monkeypatch):
    comms, caller = commander

    def review(entry):
        return CommsRuleReviewTool(entry=entry).apply(comms, caller).broken

    assert lines(comms, RulesDue) == [
        "RULES DUE (no rule review recorded for lead) -> agent-comms-commander rules review"
        " and answer the drawn rules"]
    drawn = ("Declare each fact once and derive all consumers from its owner.",
             "Kinds are class families whose declarations own their behavior and membership.")
    review(DrawnRuleReviewEntry(drawn))
    with pytest.raises(ValueError, match="file:line or P-number"):
        review(AnsweredRuleReviewEntry(1, "This tick reads every open order before it sends any new one."))
    review(AnsweredRuleReviewEntry(1, drawn[0] + " rules.py:47"))
    with pytest.raises(ValueError, match="file:line or P-number"):
        review(AnsweredRuleReviewEntry(2, drawn[1]))
    assert review(AnsweredRuleReviewEntry(2, drawn[1] + " P275")) == ()
    assert lines(comms, RulesDue) == []
    later = time.time() + RulesDue.max_age + 60
    monkeypatch.setattr(time, "time", lambda: later)
    assert lines(comms, RulesDue) == [
        "RULES DUE (last rule review was 21 min ago (max 20)) -> agent-comms-commander rules review"
        " and answer the drawn rules"]



def test_plain_messages_need_no_order_labels():
    assert DirectiveText.refused("Yes, go ahead.") == ()


def test_a_claude_ai_directive_passes_the_commanders_wording_with_no_rules_review(commander, tmp_path):
    """claude-ai directs the commanders: its sends pass the same word checks as order send and
    need no rules review."""
    comms, _ = commander
    comms.registry.declare(comms.registry.new_thread("claude-ai", frozenset(), str(tmp_path), created_at=3.0))

    def send(body):
        return CommsSendTool(sender="claude-ai", target="worker", body=body).apply(comms, CALLER)

    with pytest.raises(ValueError, match='Soft wording'):
        send('Thanks for moving the count at tools.py:829 into its owner, that is correct.')
    clean = "The count copy at tools.py:829 is wrong: delete it and read the count from its owner."
    send(clean)
    assert [(m.sender, m.body) for m in comms.bus.inbox("worker")] == [("claude-ai", clean)]

def test_the_read_tool_returns_every_members_lines(commander, standing):
    comms, caller = commander
    read = CommsStandingRulesTool().apply(comms, caller)
    assert read.broken == tuple(rule.line() for rule in StandingRule.broken(comms, "lead"))
    assert any(isinstance(rule, HuntDue) and rule.line() in read.broken
               for rule in StandingRule.broken(comms, 'lead'))


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
