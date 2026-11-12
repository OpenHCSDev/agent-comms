"""The real SessionStart command works before a teammate connects to the bus."""


import io
import json
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_comms.cli_bridges.hook_deferral import StopHook
from agent_comms.cli_bridges.hook_forks import SubagentHook
from agent_comms.cli_bridges.hook_read_gate import GatedCall, MonitorInput
from agent_comms.cli_bridges.hook_requests import (
    PrimeRequest,
    ReminderRequest,
    ResidentHookOutput,
    SkillRequest,
)
from agent_comms.cli_bridges.hook_reread import BoardReminder, DeafReminder, SliceReminder
from agent_comms.cli_bridges.session_watch import DeafWatch, WatchClaim
from agent_comms.cli_bridges.shared import Binding, GivenBinding
from agent_comms.comms import Comms
from agent_comms.coordination.database import CoordinationStore
from agent_comms.coordination.session import CoordinationSession
from agent_comms.coordination.tables.board_delivery import ReaderBoardDelivery
from agent_comms.heartbeat_subjects import TeammateSubject
from agent_comms.messaging.channel_targets import TeammatesTag
from agent_comms.nominal.field_codec import FieldCodec, OrderId
from agent_comms.nominal.text_digest import TextDigest
from agent_comms.planning.goal_actions import CreateProblemGoalAction
from agent_comms.planning.orders import (
    ProgressRead,
    ReviewedHeadVerdict,
)
from agent_comms.planning.plan_phases import BusWriterPhase
from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.repo import Repo
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.projects.rule_probes import Probe
from agent_comms.registry.registry_document import RegisteredTeamProject
from agent_comms.registry.thread_identity import RecordedThreadIncarnation
from agent_comms.registry.thread_model import DeclaredModel
from agent_comms.registry.threads import ForkThread, NativeThread
from agent_comms.storage.recorded_time import RecordedEventTime
from agent_comms.task_attachments.team_pings import RulePing, TeamDecisions, TeamUpdate
from agent_comms.team.package import TeamPackage
from agent_comms.team.pattern_sources import PostmortemFailure
from agent_comms.team.rule_deal import (
    AuditDue,
    CompactedDeal,
    OffDealMode,
    Owed,
    OwnerWordsAnswer,
    ReadSpan,
    RuleGateSettings,
    SessionAudit,
    StartedDeal,
    StartupDealMode,
    UnstampedCompactionDeal,
)
from agent_comms.team.rule_file import RuleFile
from agent_comms.team.spinning import SpinningTeammate
from agent_comms.team.standing import PlanIdle
from agent_comms.team.standing_rules import (
    DirectiveUnacted,
    OrderlessUnreviewedHead,
    RetiredStandingRule,
    ReviewTimerUp,
)
from agent_comms.transport.mcp import AnsweredCall
from agent_comms.ui_model.teammate_state import AwaitingTeam
from agent_comms_hooks.board import (
    BoardEntry,
)
from agent_comms_hooks.reread import ReminderProgram
from agent_comms_hooks.shell_commands import EmergencyHookStop
from agent_comms_hooks.skill import (
    DeckReader,
    DeckStart,
    LightPrime,
    LightReader,
    Prime,
    SessionSource,
)
from agent_comms_hooks.watch import HookBridgeUnavailable, SessionEndpoint, WatchRecord
from conftest import fresh_confession, probe_answers
from team_heartbeat_fixture import capture_frame
from test_head_review import ACCEPTED, invoke
from test_team_notifications import beat

pytest_plugins = ("test_head_review", "test_team_notifications")


def admitted_binding(comms: Comms, actor: str = "claude-code", session: str = "s") -> Binding:
    """Admit fixture context through the binding owner, not a launcher record."""
    with Binding.open(comms.root / "hook-fixture-bindings" / actor) as db:
        return GivenBinding({"root": str(comms.root), "bus_thread": actor,
                             "session": session}).bind(db).registered(comms).bind(db)


@pytest.fixture
def team_binding(team_bus: Comms) -> Binding:
    return admitted_binding(team_bus)


def evaluate(request, comms: Comms, binding: Binding):
    output = ResidentHookOutput()
    event = request.decode_event(comms, binding, output)
    return request.evaluate(event, comms, output, binding)



def session_start(transcript, source, comms: Comms, binding: Binding, **extra):
    payload = {"hook_event_name": "SessionStart", "source": source, "session_id": binding.thread,
               "transcript_path": str(transcript), **extra}
    reply = evaluate(SkillRequest(payload=payload), comms, binding)
    return subprocess.CompletedProcess("resident SessionStart", reply.status, reply.stdout, reply.stderr)


@pytest.mark.parametrize("source", ["startup", "resume", "compact"])
def test_session_wire_keeps_keyword_only_fields_and_resident_output(
        tmp_path: Path, source: str) -> None:
    transcript = tmp_path / "session.jsonl"
    output = ResidentHookOutput()
    start = DeckStart.of({
        "session_id": "session", "hook_event_name": "SessionStart",
        "transcript_path": str(transcript), "source": source, "agent_id": "child",
        "output": "foreign input cannot replace resident context",
    }, output=output)
    assert start.source == SessionSource(source)
    assert start.stem == transcript.with_suffix("") / "subagents" / "agent-child.jsonl"
    assert start.output is output


def test_light_prime_executes_through_the_resident_request(
        tmp_path: Path, team_session: Comms, team_bus: Comms,
        team_binding: Binding) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    request = PrimeRequest(payload={
        "session_id": team_binding.thread, "hook_event_name": "Prime",
        "transcript_path": str(transcript),
    }, prime=LightPrime)
    reply = evaluate(request, team_bus, team_binding)
    assert reply.status == 0, reply.stderr
    start = DeckStart.decode(request.payload)
    assert start.current().trigger == LightPrime.trigger()
    assert start.current().paths == tuple(path.resolve() for path in LightReader.sources())
    assert str(start.audit) in reply.stdout


def unbound_session_start(transcript, source):
    return subprocess.run(
        [sys.executable, "-c", "from agent_comms_hooks.watch import SkillText; SkillText.main()"],
        input=json.dumps({"hook_event_name": "SessionStart", "source": source, "session_id": "s",
                          "transcript_path": str(transcript)}),
        capture_output=True, text=True, timeout=30,
    )


@pytest.mark.parametrize("source", ["startup", "resume", "compact"])
def test_an_unbound_session_start_gets_no_team_sources(tmp_path, monkeypatch, source):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    result = unbound_session_start(transcript, source)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert not transcript.with_suffix(".deal.json").exists()


@pytest.mark.parametrize("source", ["startup", "resume", "compact"])
def test_the_team_session_start_reads_all_package_files(tmp_path, source, team_session, team_bus, team_binding):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    result = session_start(transcript, source, comms=team_bus, binding=team_binding)
    assert result.returncode == 0, result.stderr
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    trigger = {"startup": StartedDeal(), "resume": StartedDeal(),
               "compact": UnstampedCompactionDeal()}[source]
    assert context.startswith(trigger.reason() + " ")
    assert "WHAT TRISTAN ACTUALLY SAID LAST" in context
    assert ("FUCK YOUR COMPACTION" in context) == (source == "compact")
    dealt = dealt_sources(transcript)
    assert all(str(path) in context for path in dealt)
    expected = DeckReader.sources()
    assert len(dealt) == len(expected) and set(dealt) == {path.resolve() for path in expected}


@pytest.mark.parametrize("mode", (StartupDealMode, OffDealMode))
def test_configured_claude_compaction_keeps_the_deal_and_emits_no_read_order(
    tmp_path, team_bus, team_binding, mode,
):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    start = DeckStart.decode(hook_record(transcript, "SessionStart"))
    assert start.take(team_bus, team_binding)
    start.audit.write_text("retained audit")
    start.configure(RuleGateSettings(mode=mode))
    before = start.current()

    compacted = replace(start, source=SessionSource("compact"))
    assert compacted.take(team_bus, team_binding) == ()
    assert start.current() == before
    assert start.audit.read_text() == "retained audit"


def dealt_sources(transcript):
    """The files the last deal named, read from its one record."""
    return DeckStart.decode(hook_record(transcript, "SessionStart")).current().dealt


def hook_record(transcript, event="PostToolUse"):
    return {"hook_event_name": event, "session_id": "s", "transcript_path": str(transcript),
            "tool_name": "Bash", "tool_input": {}}


def unbound_reminders(transcript, monkeypatch, capsys):
    for _ in range(SliceReminder.every * 2):
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(hook_record(transcript))))
        ReminderProgram.main()
        assert capsys.readouterr().out == ""


def test_no_deck_no_slices_outside_a_team_session(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    transcript = tmp_path / "session.jsonl"
    unbound_reminders(transcript, monkeypatch, capsys)
    assert not transcript.with_suffix(".calls").exists()


def test_a_slice_every_few_calls_rotating_and_dealt_again_at_compaction(tmp_path, team_session, team_bus, team_binding):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    start = DeckStart.decode(hook_record(transcript, "SessionStart"))
    assert start.take(team_bus, team_binding)
    hook = SliceReminder.decode(hook_record(transcript))
    slices = DeckReader.slices()
    taken = [hook.take(team_bus, team_binding, hook.count_call()) for _ in range(SliceReminder.every * 3)]
    assert taken[SliceReminder.every - 1::SliceReminder.every] == [(text,) for text in slices[:3]]
    assert sum(map(len, taken)) == 3
    assert start.take(team_bus, team_binding)  # compaction deals the deck again from its start
    assert [hook.take(team_bus, team_binding, hook.count_call()) for _ in range(SliceReminder.every)][-1] == (slices[0],)


@pytest.mark.parametrize("actor", ("claude-code", "unit-x"))
def test_rearm_rides_every_tool_call_while_a_running_bridge_has_no_monitor(
        tmp_path: Path, team_session, team_bus: Comms, actor: str):
    """A tagged teammate and an untagged peer share the re-arm behaviour."""
    binding = admitted_binding(team_bus, actor)
    hook = DeafReminder.decode(hook_record(tmp_path / "session.jsonl"))
    claim = WatchClaim(hook.session_id)
    deaf = DeafWatch("now (no Monitor holds its reader claim)").line()
    assert deaf not in hook.take(team_bus, binding, hook.count_call())
    with claim.claim.claimed():
        assert deaf in hook.take(team_bus, binding, hook.count_call())
        assert deaf in hook.take(team_bus, binding, hook.count_call())
        with claim.reader_held():
            assert deaf not in hook.take(team_bus, binding, hook.count_call())


def test_an_unbound_session_is_never_told_to_rearm_a_team_watch(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    transcript = tmp_path / "session.jsonl"
    unbound_reminders(transcript, monkeypatch, capsys)
    hook = DeafReminder.decode(hook_record(transcript))
    assert not hook.deck(DeckReader()).exists()


@pytest.mark.parametrize(("command", "allowed"), (
    ("agent-comms-claude-bridge watch", True),
    ("/owned/bin/agent-comms-claude-bridge watch --state '/owned/state directory'", True),
    ("echo unrelated", False),
    ("agent-comms-claude-bridge connect", False),
    ("agent-comms-claude-bridge watch; echo unrelated", False),
    ("agent-comms-claude-bridge watch > output", False),
    ("agent-comms-claude-bridge watch --root other", False),
    ("agent-comms-claude-bridge watch --state", False),
))
def test_only_the_bus_monitor_may_cross_an_unfinished_deal(command: str, allowed: bool):
    assert MonitorInput(command).watches_bus() is allowed


def test_loading_the_monitors_schema_may_cross_an_unfinished_deal(
        dealt_session, capsys, team_session, team_binding):
    """The gate permits the bus watch, so it cannot refuse the one call that makes it callable."""
    transcript = dealt_session[0]
    assert not refused(capsys, gated(transcript, "ToolSearch", {"query": "select:Monitor"},
                                     comms=team_session, binding=team_binding))
    assert refused(capsys, gated(transcript, "Bash", {"command": "true"},
                                 comms=team_session, binding=team_binding))


def tool_call(hook, comms):
    """One tool call's board lines, as the resident answers its hook: the deck slices ride the
    same call and are not the board."""
    taken = hook.take(comms, admitted_binding(comms, "colleague"), hook.count_call())
    return tuple(line for line in taken if line.startswith("BOARD"))


def delivered(comms):
    """How many of the colleague's rows Core holds as delivered."""
    lookup = ReaderBoardDelivery.column(comms.registry.require("colleague").incarnation.require_recorded())
    with CoordinationSession(str(CoordinationStore.path_in(comms.root))) as store, store.writing() as db:
        return len(ReaderBoardDelivery.select(db, reader=lookup))


def board_at(comms, now):
    """The colleague's tool-call rows at ``now``, through the one delivery owner."""
    return ReaderBoardDelivery.lines(comms, comms.registry.require("colleague").incarnation, now)


def test_a_recorded_verdict_drops_its_board_line_on_the_next_tool_call(
        head_bus, notification_bus, tmp_path, monkeypatch):
    """2026-10-25: JUDGE #3311 and #3331 kept printing after `judge ok` until the next packet,
    because the hook reprinted the lines the last packet stored. The board is now derived from
    Core when the hook asks: the verdict drops the line on the next tool call, and a line two
    packets carry prints once."""
    comms, _, request = head_bus
    # A relative HOME keeps the real AF_UNIX endpoint below its path-length limit.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", "home")
    hook = BoardReminder.decode(hook_record(tmp_path / "session.jsonl"))
    claim = WatchClaim(hook.session_id)
    claim.path.parent.mkdir(parents=True)
    record = WatchRecord("colleague", str(comms.root), ("watch",), tmp_path / "inbox")
    worker = RecordedThreadIncarnation("worker", 1.0)
    colleague = comms.registry.require("colleague").incarnation
    rules = (*OrderlessUnreviewedHead.scan_reviews(comms.goals, ProgressRead(())),
             ReviewTimerUp(holder=colleague, problem="P1", order="order-P1",
                           since=RecordedEventTime(1000.0)),
             SpinningTeammate(holder=worker, problem="P2", call_count=92))
    beat(comms, rules)
    beat(comms, rules)
    head_line = f"--pr {request.pr} --head {request.head}"

    def shared_board() -> tuple[str, ...]:
        return tuple(entry.text for packet in TeamUpdate.latest(comms, colleague)
                     for entry in packet.board(comms))
    with claim.held(record), claim.reader_held():
        assert DeckStart.decode(hook_record(tmp_path / "session.jsonl", "SessionStart")).take(comms, admitted_binding(comms, "colleague"))
        # Only the row naming this session rides its tool call; the unowned head and the
        # worker's row stay on the shared board.
        board = tool_call(hook, comms)
        assert len(board) == 1 and "P1" in board[0]
        assert len(shared_board()) == len(rules)
        assert any(head_line in line for line in shared_board())
        invoke(comms, request, ReviewedHeadVerdict(ACCEPTED))
        assert not any(head_line in line for line in shared_board())
        assert tool_call(hook, comms) == ()


def test_board_start_line_disappears_when_its_phase_order_is_sent(notification_bus, tmp_path, monkeypatch):
    comms = notification_bus
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", "home")
    hook = BoardReminder.decode(hook_record(tmp_path / "session.jsonl"))
    claim = WatchClaim(hook.session_id)
    claim.path.parent.mkdir(parents=True)
    record = WatchRecord("colleague", str(comms.root), ("watch",), tmp_path / "inbox")
    reading = capture_frame(comms, ProgressRead()).plan
    beat(comms, (PlanIdle(BusWriterPhase, reading),))
    phase_name = f"{BusWriterPhase.plan.declared_name}/{BusWriterPhase.declared_name}"
    colleague = comms.registry.require("colleague").incarnation
    request = comms.goals.todos.add_request(colleague, "Start the ready bus writer phase").id
    original = comms.goals.todos.get("P1").order.checked()
    with claim.held(record), claim.reader_held():
        assert DeckStart.decode(hook_record(tmp_path / "session.jsonl", "SessionStart")).take(comms, admitted_binding(comms, "colleague"))
        def shared_board() -> tuple[str, ...]:
            return tuple(entry.text for packet in TeamUpdate.latest(comms, colleague)
                         for entry in packet.board(comms))

        # A ready phase names nobody: it stays on the shared board, off every tool call.
        assert tool_call(hook, comms) == ()
        assert sum(phase_name in line for line in shared_board()) == 1
        # The worker holds P1 and P2 already; the idle outsider takes the phase order.
        comms.goals.update_goal("outsider", CreateProblemGoalAction(
            problem="P3", request=request, assigner=colleague,
            repo=RepositoryName.from_wire("OpenHCSDev/agent-comms"),
            text="Start the ready bus writer phase", review_at=RecordedEventTime(time.time() + 3600),
            order=replace(original, id=OrderId.mint(), serves=BusWriterPhase,
                          at=RecordedEventTime(time.time())),
        ))
        assert sum(phase_name in line for line in shared_board()) == 0
        assert tool_call(hook, comms) == ()


def test_a_board_row_rides_a_tool_call_as_one_short_line():
    long = BoardEntry("long", "ADVANCE request " + "x" * 400, 0.0, 0.0)
    line = long.line(120.0)
    assert line.startswith("BOARD: ADVANCE request x") and line.endswith("… (2 min)")
    assert len(line) < BoardEntry.shown_chars + 20


def test_a_notice_naming_a_deleted_rule_decodes_as_history():
    """2026-11-02: deleting BlockedTalk stopped two inbox drains on old notices naming it."""
    since = RecordedEventTime(1.0)
    worker = TeammateSubject(RecordedThreadIncarnation("worker", 1.0))
    stored = FieldCodec.encode(RulePing(ReviewTimerUp, TextDigest.of("t"), text="Waiting on root",
                                        subjects=(worker,), since=since))
    stored["rule"] = "blocked_talk"
    ping = FieldCodec.decode(RulePing, stored)
    assert ping.rule is RetiredStandingRule and ping.text == "Waiting on root"
    assert not ping.urgent and not ping.team_disposes and not ping.waits_on_team(2.0)  # never on the board


def test_rules_carrying_his_words_flag_their_board_lines():
    since = RecordedEventTime(1.0)
    worker = TeammateSubject(RecordedThreadIncarnation("worker", 1.0))
    flagged = RulePing(DirectiveUnacted, TextDigest.of("d"), text="ACT", subjects=(worker,), since=since)
    plain = RulePing(ReviewTimerUp, TextDigest.of("t"), text="Review", subjects=(worker,),
                     since=since, threads=("worker",))
    assert flagged.addresses("colleague") and not plain.addresses("colleague")
    assert plain.addresses("worker")


def test_a_tool_call_prints_only_new_rows_naming_its_session(
        notification_bus, tmp_path, monkeypatch):
    """Retained observations and the one Core delivery owner; publication stays muted."""
    from agent_comms.registry.heartbeat_delivery import OffHeartbeatDelivery
    from agent_comms.task_attachments.team_pings import TeamUpdateStore

    comms = notification_bus
    comms.threads.set_heartbeat_delivery("colleague", OffHeartbeatDelivery)
    assert comms.registry.require("colleague").heartbeat_delivery is OffHeartbeatDelivery
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", "home")
    hook = BoardReminder.decode(hook_record(tmp_path / "session.jsonl"))
    claim = WatchClaim(hook.session_id)
    claim.path.parent.mkdir(parents=True)
    record = WatchRecord("colleague", str(comms.root), ("watch",), tmp_path / "inbox")
    colleague = comms.registry.require("colleague").incarnation
    worker = comms.registry.require("worker").incarnation
    began = time.time()
    first = RulePing(SpinningTeammate, TextDigest.of("first"), text="Read worker P1",
                     subjects=(TeammateSubject(worker),), since=RecordedEventTime(began),
                     threads=("colleague",))
    second = RulePing(ReviewTimerUp, TextDigest.of("second"), text="Review worker P2",
                      subjects=(TeammateSubject(worker),), since=RecordedEventTime(began),
                      threads=("colleague",))
    elsewhere = RulePing(ReviewTimerUp, TextDigest.of("elsewhere"), text="Review worker P3",
                         subjects=(TeammateSubject(worker),), since=RecordedEventTime(began),
                         threads=("worker",))
    TeamUpdateStore.record(comms.root, TeamDecisions(
        colleague, (first, second, elsewhere), (first, second, elsewhere), ()))
    with claim.held(record), claim.reader_held():
        assert DeckStart.decode(hook_record(tmp_path / "session.jsonl", "SessionStart")).take(comms, admitted_binding(comms, "colleague"))
        assert board_at(comms, began + 90) == (
            "BOARD: Read worker P1 (1 min)",
            "BOARD: Review worker P2 (1 min)",
        )
        assert board_at(comms, began + 91) == ()
        changed = replace(first, observation=TextDigest.of("changed"), since=RecordedEventTime(began + 90))
        TeamUpdateStore.record(comms.root, TeamDecisions(
            colleague, (changed, second), (changed,), ()))
        assert board_at(comms, began + 92) == ("BOARD: Read worker P1 (0 min)",)
        # A fresh packet does not reset delivery, and the answer window rings only once.
        TeamUpdateStore.record(comms.root, TeamDecisions(
            colleague, (changed, second), (), ()))
        alarm = began + AwaitingTeam.seconds
        assert board_at(comms, alarm + 1) == ("BOARD: Review worker P2 (5 min)",)
        assert board_at(comms, alarm + 2) == ()
        assert delivered(comms) == 2
        TeamUpdateStore.record(comms.root, TeamDecisions(colleague, (), (), ()))
        assert board_at(comms, alarm + 3) == ()
        assert delivered(comms) == 0


def test_a_verb_reply_carries_the_new_rows_naming_its_caller(notification_bus):
    """P1213: a cloud session acting through ``fork`` has no hook that knows its name, so its
    own verbs carry its rows, beside the reply and never inside it: the structured reply stays
    the declared result remote cores decode. A row prints once; a read carries no board."""
    from agent_comms.task_attachments.team_pings import TeamUpdateStore
    from agent_comms.transport.tool_caller import ThreadCaller
    from agent_comms.transport.tools.orders import CommsTodoListTool
    from agent_comms.transport.tools.send import CommsSendTool

    comms = notification_bus
    colleague = comms.registry.require("colleague").incarnation
    worker = comms.registry.require("worker").incarnation
    began = time.time()
    mine = RulePing(ReviewTimerUp, TextDigest.of("mine"), text="Review worker P2",
                    subjects=(TeammateSubject(worker),), since=RecordedEventTime(began),
                    threads=("colleague",))
    theirs = RulePing(ReviewTimerUp, TextDigest.of("theirs"), text="Review worker P3",
                      subjects=(TeammateSubject(worker),), since=RecordedEventTime(began),
                      threads=("worker",))
    TeamUpdateStore.record(comms.root, TeamDecisions(colleague, (mine, theirs), (mine, theirs), ()))
    caller = ThreadCaller("colleague")

    def answered(tool, arguments) -> AnsweredCall:
        request = tool.decoded(comms, arguments)
        return AnsweredCall(request.replied(comms, caller), request.board(comms, caller))

    # A send to the row's own subject is the action that disposes it, so speak to someone else.
    send = {"to": "outsider", "body": "Reading P2 now"}
    first = answered(CommsSendTool, send)
    assert first.board == ("BOARD: Review worker P2 (0 min)",) and "board" not in first.reply
    result = first.result()
    assert [content.text for content in result.content[1:]] == list(first.board)
    assert result.structured_content == first.reply
    assert answered(CommsSendTool, send).board == ()
    assert answered(CommsTodoListTool, {"thread": "colleague"}).board == ()


def test_a_busy_core_writer_never_fails_a_verb_that_landed(notification_bus):
    """#4088 send-back: the board runs after the verb's own write. When another writer holds
    coordination.sqlite3, that call carries no rows instead of raising, so a send that landed
    never comes back as a failure, and the rows stay unstamped for the next call."""
    import sqlite3

    from agent_comms.coordination.database import CoordinationStore
    from agent_comms.task_attachments.team_pings import TeamUpdateStore
    from agent_comms.transport.tool_caller import ThreadCaller
    from agent_comms.transport.tools.send import CommsSendTool

    comms = notification_bus
    colleague = comms.registry.require("colleague").incarnation
    worker = comms.registry.require("worker").incarnation
    row = RulePing(ReviewTimerUp, TextDigest.of("busy"), text="Review worker P4",
                   subjects=(TeammateSubject(worker),), since=RecordedEventTime(time.time()),
                   threads=("colleague",))
    TeamUpdateStore.record(comms.root, TeamDecisions(colleague, (row,), (row,), ()))
    caller = ThreadCaller("colleague")
    send = {"to": "outsider", "body": "landing while Core is busy"}

    def answered() -> AnsweredCall:
        request = CommsSendTool.decoded(comms, send)
        return AnsweredCall(request.replied(comms, caller), request.board(comms, caller))

    holder = sqlite3.connect(CoordinationStore.path_in(comms.root), timeout=0)
    holder.execute("BEGIN IMMEDIATE")
    try:
        busy = answered()
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert busy.board == () and busy.reply
    assert answered().board == ("BOARD: Review worker P4 (0 min)",)


def test_the_deck_carries_the_voice_every_fourth_slice_every_rule_reminder_and_every_failure():
    slices = DeckReader.slices()
    voice = DeckReader.paragraphs(TeamPackage.voice().read_text())
    patterns = DeckReader.paragraphs(TeamPackage.patterns().read_text())
    assert set(slices[0::4]) == set(voice)
    assert set(slices[1::4]) == set(patterns)
    assert set(slices[2::4]) == {rule.reminder for rule in RuleFile.packaged()}
    assert set(slices[3::4]) == {
        f"Recorded failure {line}" for line in PostmortemFailure.catalog().splitlines()}
    assert all(len(text) <= DeckReader.slice_chars for text in slices)


def run_resident(monkeypatch, capsys, transcript, comms: Comms, binding: Binding):
    """Direct resident evaluation; root separately qualifies the actual installed socket."""
    def run(program):
        event, = program.events
        reply = evaluate(program(payload=hook_record(transcript, event)), comms, binding)
        assert reply.status == 0, reply.stderr
        return reply.stdout
    return run


def test_the_resident_commands_order_the_reads_once_then_put_slices_inline(
        tmp_path, monkeypatch, capsys, team_session, team_bus, team_binding):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    run = run_resident(monkeypatch, capsys, transcript, comms=team_bus, binding=team_binding)
    assert run(SkillRequest)
    outputs = [run(ReminderRequest) for _ in range(SliceReminder.every)]
    assert outputs[:-1] == [""] * (SliceReminder.every - 1)
    context = json.loads(outputs[-1])["hookSpecificOutput"]["additionalContext"]
    assert context == DeckReader.slices()[0]
    assert "Read each of these" not in context


def test_a_session_that_becomes_team_is_dealt_its_sources_on_its_next_tool_call(
        tmp_path, monkeypatch, capsys, team_bus, team_binding):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    run = run_resident(monkeypatch, capsys, transcript, comms=team_bus, binding=team_binding)
    assert unbound_session_start(transcript, "startup").stdout == ""
    WatchClaim("s").enrol(WatchRecord("claude-code", str(team_bus.root), (), tmp_path / "state"))
    assert "Read each of these" in run(ReminderRequest)
    assert transcript.with_suffix(".deck.json").exists()


@pytest.mark.parametrize("actor", ("claude-code", "unit-x"))
def test_each_bound_peer_gets_the_shared_sources_and_reminders(
        tmp_path, monkeypatch, capsys, team_bus, actor):
    """Lead and untagged peer use one source list, audit and reminder deck."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    WatchClaim("s").enrol(WatchRecord(actor, str(team_bus.root), (), tmp_path / "state"))
    run = run_resident(monkeypatch, capsys, transcript, comms=team_bus, binding=admitted_binding(team_bus, actor))
    context = json.loads(run(SkillRequest))["hookSpecificOutput"]["additionalContext"]
    assert "Read each of these" in context and "What the registered boards hold:" in context
    assert "FUCK YOUR COMPACTION" not in context
    assert "WHAT TRISTAN ACTUALLY SAID LAST" in context
    outputs = [run(ReminderRequest) for _ in range(SliceReminder.every)]
    assert outputs[:-1] == [""] * (SliceReminder.every - 1)
    context = json.loads(outputs[-1])["hookSpecificOutput"]["additionalContext"]
    assert context == DeckReader.slices()[0]
    assert transcript.with_suffix(".deck.json").exists()
    assert set(dealt_sources(transcript)) == {path.resolve() for path in DeckReader.sources()}


@pytest.mark.parametrize("path", TeamPackage.rules_files(), ids=lambda path: path.name)
def test_package_rule_opens_with_its_wakeup_then_its_reason(path):
    rule = RuleFile.read(path)
    assert rule.why and rule.wakeup.startswith(">")
    assert rule.render().startswith(rule.wakeup + "\n\n" + rule.summary)
    assert rule.reminder.startswith(rule.wakeup) and rule.reminder.endswith(rule.drift)


def test_rule_file_without_a_wakeup_is_refused(tmp_path):
    source = TeamPackage.rules_files()[0].read_text(encoding="utf-8")
    path = tmp_path / "asleep.md"
    path.write_text("".join(line for line in source.splitlines(keepends=True)
                            if not line.startswith(">")), encoding="utf-8")
    with pytest.raises(ValueError, match="wake-up"):
        RuleFile.read(path)


def test_failure_catalog_numbers_every_evidenced_member_once():
    members = sorted(PostmortemFailure.members_with(PostmortemFailure), key=lambda member: member.number)
    assert [member.number for member in members] == list(range(1, len(members) + 1))
    assert all(member.evidence for member in members)


def test_the_reading_order_names_exactly_the_packaged_rules_and_the_spine_leads():
    names = tuple(RuleFile.read(path).name for path in TeamPackage.rules_files())
    assert sorted(TeamPackage.reading_order) == sorted(names)
    assert tuple(rule.name for rule in RuleFile.packaged()) == TeamPackage.reading_order
    assert TeamPackage.reading_order[0] == "spine"


def test_every_full_deal_reads_the_paper_before_any_rule():
    sources = DeckReader.sources()
    paper = TeamPackage.paper()
    assert paper and sources[:len(paper)] == paper
    assert sources[len(paper)] == RuleFile.packaged()[0].path


def test_the_prime_variants_cover_papers_and_patterns_once_each_with_the_patterns_last():
    """/prime deals a selection of the one reader list: each variant keeps or drops the papers
    and the pattern catalog, every rule stays, and a kept catalog stays last."""
    paper = set(TeamPackage.paper())
    patterns = TeamPackage.patterns()
    kept = set()
    for prime in Prime.members_with(Prime):
        sources = prime.reader.sources()
        assert all(rule.path in sources for rule in DeckReader.rules())
        kept.add((paper <= set(sources), patterns in sources))
        if patterns in sources:
            assert prime.reader.sources()[-1] == patterns
        assert prime.reader.slices()
    assert kept == {(True, True), (True, False), (False, True), (False, False)}


def test_every_prime_variant_says_it_was_primed_never_that_the_session_started():
    for prime in Prime.members_with(Prime):
        reason = prime.trigger().reason()
        assert f"/prime ({prime.declared_name})" in reason
        assert reason != StartedDeal().reason()


def test_prime_in_a_session_no_bus_thread_holds_is_refused_out_loud(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    with pytest.raises(HookBridgeUnavailable, match="loose"):
        SessionEndpoint("loose").session().require_bound()


def test_changing_the_team_project_preserves_the_peer_deal_and_real_child_delegation(
        tmp_path, capsys, team_session, team_bus, team_binding):
    """Changing the team's repositories neither demotes a peer nor changes its retained
    context."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    assert DeckStart.decode(hook_record(transcript, "SessionStart")).take(team_bus, team_binding)
    assert transcript.with_suffix(".deck.json").exists()
    team_session.registry.set_team_project(RegisteredTeamProject((Repo(
        checkout=tmp_path, github=RepositoryName.from_wire("owner/repo")),)))
    dealt = transcript.with_suffix(".deal.json").read_bytes()
    reminder = BoardReminder.decode(hook_record(transcript))
    assert [reminder.take(team_bus, team_binding, reminder.count_call()) for _ in range(SliceReminder.every - 1)] == [()] * (SliceReminder.every - 1)
    assert transcript.with_suffix(".deck.json").exists()
    assert transcript.with_suffix(".deal.json").read_bytes() == dealt
    StopHook.decode({"hook_event_name": "Stop", "session_id": "s",
                     "transcript_path": str(transcript), "stop_hook_active": False}, comms=team_bus, binding=team_binding).guard()
    assert capsys.readouterr().out == ""
    SubagentHook.decode({"hook_event_name": "SubagentStart", "session_id": "s",
                         "transcript_path": str(transcript), "agent_id": "a9",
                         "agent_type": "Explore"}).register(team_bus, team_binding)
    assert "in-turn fork claude-a9" in capsys.readouterr().out
    child = team_session.registry.require_member("claude-a9", ForkThread)
    assert tuple(child.parent.names()) == ("claude-code",)
    assert transcript.with_suffix(".deck.json").exists()


def test_a_fork_registers_under_the_session_that_spawned_it(
        tmp_path, capsys, team_session, team_bus, team_binding):
    """No CLI subprocess: the hook declares the fork in its own process as a child of the
    session's own bus thread, carrying every tag that thread carries (#teammates included)."""
    team_session.registry.declare(NativeThread(
        "claude-nra", frozenset({TeammatesTag.declared_name}), Checkout(tmp_path),
        model=DeclaredModel("anthropic/claude-opus-4-6")))
    WatchClaim("s2").enrol(WatchRecord(
        "claude-nra", str(team_session.root), (), tmp_path / "state2"))
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    for session, agent, actor in (("s", "a7", "claude-code"), ("s2", "a8", "claude-nra")):
        SubagentHook.decode({"hook_event_name": "SubagentStart", "session_id": session,
                             "transcript_path": str(transcript), "agent_id": agent,
                             "agent_type": "fork"}).register(
                                 team_session, admitted_binding(team_session, actor, session))
        assert f"in-turn fork claude-{agent}" in capsys.readouterr().out
    snapshot = team_session.registry.snapshot()
    for agent, parent in (("a7", "claude-code"), ("a8", "claude-nra")):
        fork = snapshot.require_member(f"claude-{agent}", ForkThread)
        assert tuple(fork.parent.names()) == (parent,)
        assert fork.tags == snapshot.require(parent).tags
    assert TeammatesTag.declared_name in snapshot.require("claude-a8").tags
    assert {"claude-a7", "claude-a8"}.isdisjoint(
        thread.name for thread in snapshot.team_project.colleagues(snapshot))


def gated(transcript, tool, tool_input, *, comms: Comms, binding: Binding, **extra):
    return GatedCall.of({"hook_event_name": "PreToolUse", "session_id": "s",
                         "transcript_path": str(transcript), "tool_name": tool,
                         "tool_input": tool_input, **extra}, comms=comms, binding=binding)


def refused(capsys, call):
    call.judge()
    out = capsys.readouterr().out
    return out and json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def answered_audit(owner_words=()):
    """The audit answering the owner's words and the probes, as a session writes it."""
    return json.dumps({"owner_words": list(owner_words),
                       "probes": probe_answers(tuple(entry["words"] for entry in owner_words))})


@pytest.fixture
def dealt_session(tmp_path, team_session, team_bus, team_binding):
    """A teammate session just dealt its deck: its transcript and the deal."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    start = DeckStart.decode(hook_record(transcript, "SessionStart"))
    assert start.take(team_bus, team_binding)
    return transcript, start


def test_core_gate_lets_the_emergency_hook_stop_through_and_names_it(
        capsys, dealt_session, team_bus, team_binding):
    """A gate refusing every tool cannot refuse the way out of itself, and says where it is."""
    transcript, _ = dealt_session
    stop = "agent-comms-hooks stop --from someone --why 'the gate is wrong'"
    assert not refused(capsys, gated(transcript, "Bash", {"command": stop},
                                     comms=team_bus, binding=team_binding))
    assert refused(capsys, gated(transcript, "Bash", {"command": f"{stop}; ls"},
                                 comms=team_bus, binding=team_binding))
    gated(transcript, "Bash", {"command": "ls"}, comms=team_bus, binding=team_binding).judge()
    assert EmergencyHookStop.way_out in json.loads(
        capsys.readouterr().out)["hookSpecificOutput"]["permissionDecisionReason"]


def read_whole(transcript, source, *, comms: Comms, binding: Binding):
    gated(transcript, "Read", {"file_path": str(source)}, comms=comms, binding=binding).completed()


def test_a_read_batch_opens_the_next_with_no_answer_owed(capsys, dealt_session, team_bus, team_binding):
    """Reading is the whole price of opening the next batch: the answers are the probes, owed
    once at the end, never a per-rule paraphrase between batches."""
    transcript, start = dealt_session
    for paper in TeamPackage.paper():
        read_whole(transcript, paper.resolve(), comms=team_bus, binding=team_binding)
    rules = DeckReader.rules()
    for rule in rules[:start.current().read_batch]:
        read_whole(transcript, rule.path.resolve(), comms=team_bus, binding=team_binding)
    assert not refused(capsys, gated(transcript, "Read", {"file_path": str(rules[start.current().read_batch].path)}, comms=team_bus, binding=team_binding))
    assert refused(capsys, gated(transcript, "Bash", {"command": "ls"}, comms=team_bus, binding=team_binding))  # the rest stay unread


def test_a_parallel_batch_opens_whichever_order_its_hooks_reach_the_lock(capsys, dealt_session, team_bus, team_binding):
    """Each read of one call runs its own hook, and they reach the lock in any order: on
    2026-10-30 a batch of four sent in dealt order was refused because read three got there
    before read two, and Tristan was reduced to one read per call."""
    transcript, start = dealt_session
    for paper in TeamPackage.paper():
        read_whole(transcript, paper.resolve(), comms=team_bus, binding=team_binding)
    after = DeckReader.sources()[len(TeamPackage.paper()):]
    for source in reversed(after[:start.current().read_batch]):
        assert not refused(capsys, gated(transcript, "Read", {"file_path": str(source.resolve())}, comms=team_bus, binding=team_binding))
    # Reads in flight count as opened: the batch after them opens, one further does not.
    assert refused(capsys, gated(transcript, "Read", {"file_path": str(after[start.current().read_batch * 2].resolve())}, comms=team_bus, binding=team_binding))
    for source in reversed(after[start.current().read_batch:start.current().read_batch * 2]):
        assert not refused(capsys, gated(transcript, "Read", {"file_path": str(source.resolve())}, comms=team_bus, binding=team_binding))


def test_a_whole_paper_opens_in_one_parallel_batch(capsys, dealt_session, team_bus, team_binding):
    """Every part of a paper is judged before any read of it completes, so the whole paper and
    the first rules read in one call."""
    transcript, start = dealt_session
    papers = TeamPackage.paper()
    for part in papers:
        assert not refused(capsys, gated(transcript, "Read", {"file_path": str(part.resolve())}, comms=team_bus, binding=team_binding))
    rules = DeckReader.rules()
    for rule in rules[:start.current().read_batch]:
        assert not refused(capsys, gated(transcript, "Read", {"file_path": str(rule.path)}, comms=team_bus, binding=team_binding))
    beyond = DeckReader.sources()[len(papers) + start.current().read_batch * 2]
    assert refused(capsys, gated(transcript, "Read", {"file_path": str(beyond.resolve())}, comms=team_bus, binding=team_binding))


def test_dealt_files_open_in_order_with_the_patterns_last(capsys, dealt_session, team_bus, team_binding):
    """On 2026-10-30 a session opened the closing file and two rules first and never read the
    papers: the next batch of dealt files opens, any later one is refused and named the next."""
    transcript, start = dealt_session
    dealt = DeckReader.sources()
    assert dealt[-1].stem == "patterns"
    papers = TeamPackage.paper()
    past_batch = papers[start.current().read_batch]
    for later in (TeamPackage.patterns(), DeckReader.rules()[0].path, past_batch):
        call = gated(transcript, "Read", {"file_path": str(later.resolve())}, comms=team_bus, binding=team_binding)
        call.judge()
        reason = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
        assert reason["permissionDecision"] == "deny"
        assert f"batches of {start.current().read_batch}" in reason["permissionDecisionReason"]
        assert "Next batch: " + ", ".join(
            str(paper.resolve()) for paper in papers[:start.current().read_batch]
        ) in reason["permissionDecisionReason"]
    assert not refused(capsys, gated(transcript, "Read", {"file_path": str(papers[0].resolve())}, comms=team_bus, binding=team_binding))
    read_whole(transcript, papers[0].resolve(), comms=team_bus, binding=team_binding)
    assert not refused(capsys, gated(transcript, "Read", {"file_path": str(papers[1].resolve())}, comms=team_bus, binding=team_binding))


def test_the_full_audit_and_the_owners_words_open_the_gate(capsys, tmp_path, team_session, team_bus, team_binding):
    transcript = tmp_path / "session.jsonl"
    words = ("fix the gate",)
    transcript.write_text(json.dumps({"type": "user", "message": {"content": words[0]}}) + "\n")
    start = DeckStart.decode(hook_record(transcript, "SessionStart"))
    start.take(team_bus, team_binding)
    start.prepare(DeckReader(), AuditDue(words), StartedDeal())
    for source in DeckReader.sources():
        read_whole(transcript, source.resolve(), comms=team_bus, binding=team_binding)
    start.audit.write_text(answered_audit())
    assert refused(capsys, gated(transcript, "Bash", {"command": "ls"}, comms=team_bus, binding=team_binding))  # his words unanswered
    answers = ({"words": words[0], "doing": "editing rule_deal"},)
    start.audit.write_text(answered_audit(owner_words=answers))
    assert not refused(capsys, gated(transcript, "Bash", {"command": "ls"}, comms=team_bus, binding=team_binding))


def test_the_deal_prints_its_template_once(
        tmp_path, capsys, team_session, team_bus, team_binding):
    """The hook's order prints the one template; the first refusal does not print it again."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    said = DeckStart.decode(hook_record(transcript, "SessionStart")).take(team_bus, team_binding)
    template = next(part for part in said if "{\n" in part)
    assert {"owner_words", "meant", "probes"} <= json.loads(template[template.index("{\n"):]).keys()
    gated(transcript, "Bash", {"command": "true"}, comms=team_bus, binding=team_binding).judge()
    assert "{\n" not in json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecisionReason"]


def test_every_tool_but_read_waits_for_each_dealt_rule_file_then_its_audit(
        tmp_path, capsys, team_session, team_bus, team_binding):
    """The deal names the packaged rule files themselves; PreToolUse only judges, and a file
    counts as read only when a Read covering all of it completes (PostToolUse). Then only the
    audit may be written until it answers every dealt rule."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    assert DeckStart.decode(hook_record(transcript, "SessionStart")).take(team_bus, team_binding)
    parts = list(dealt_sources(transcript))
    assert set(parts) == {source.resolve() for source in DeckReader.sources()}
    assert refused(capsys, gated(transcript, "Bash", {"command": "ls"}, comms=team_bus, binding=team_binding))
    whole = {"file_path": str(parts[0])}
    assert not refused(capsys, gated(transcript, "Read", whole, comms=team_bus, binding=team_binding))
    assert refused(capsys, gated(transcript, "Read", {"file_path": str(transcript)}, comms=team_bus, binding=team_binding))  # not dealt
    assert refused(capsys, gated(transcript, "Edit", {"file_path": str(transcript)}, comms=team_bus, binding=team_binding))  # a Read that never completed
    gated(transcript, "Read", {**whole, "limit": 5}, comms=team_bus, binding=team_binding).completed()
    assert refused(capsys, gated(transcript, "Edit", {"file_path": str(transcript)}, comms=team_bus, binding=team_binding))  # a partial read is not a reading
    for part in parts:
        gated(transcript, "Read", {"file_path": str(part)}, comms=team_bus, binding=team_binding).completed()
    audit = transcript.with_suffix(".audit.json")
    assert refused(capsys, gated(transcript, "Bash", {"command": "ls"}, comms=team_bus, binding=team_binding))  # no audit yet
    assert not refused(capsys, gated(transcript, "Write", {"file_path": str(audit)}, comms=team_bus, binding=team_binding))
    audit.write_text(json.dumps({"owner_words": []}))
    assert refused(capsys, gated(transcript, "Bash", {"command": "ls"}, comms=team_bus, binding=team_binding))  # read, probes unanswered
    audit.write_text(answered_audit())
    assert not refused(capsys, gated(transcript, "Bash", {"command": "ls"}, comms=team_bus, binding=team_binding))
    assert DeckStart.decode(hook_record(transcript, "SessionStart")).take(team_bus, team_binding)  # compaction
    assert refused(capsys, gated(transcript, "Bash", {"command": "ls"}, comms=team_bus, binding=team_binding))


def test_refusal_counts_names_tool_and_prefills_the_owned_audit(
        tmp_path: Path, capsys: pytest.CaptureFixture[str], team_bus, team_binding) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    start = DeckStart.decode(hook_record(transcript, "SessionStart"))
    words = ("Read the rules.\nThen write the audit.",)
    start.prepare(DeckReader(), AuditDue(words), StartedDeal())
    reasons = []
    for attempt, tool in enumerate(("Bash", "Edit"), 1):
        gated(transcript, tool, {"file_path": str(transcript), "command": "true"}, comms=team_bus, binding=team_binding).judge()
        output = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        reason = output["permissionDecisionReason"]
        assert reason.startswith(Owed.nudges[attempt - 1])
        assert f"{tool} waits on reading." in reason
        reasons.append(reason)
    assert f"Tristan told you, verbatim:\n{words[0]}\nYou did not." in reasons[0]
    assert StartedDeal().reason() in reasons[0] and StartedDeal().reason() not in reasons[1]
    assert "{\n" not in reasons[1]  # the template and his words print once per deal
    template = FieldCodec.decode(SessionAudit, json.JSONDecoder().raw_decode(reasons[0][reasons[0].index("{\n"):])[0])
    for source in DeckReader.sources():
        gated(transcript, "Read", {"file_path": str(source)}, comms=team_bus, binding=team_binding).completed()
    due = start.current().due
    assert tuple(answer.words for answer in template.owner_words) == words
    assert all(not answer.doing for answer in template.owner_words)
    assert due.refusal(template)  # the blank template cannot count as the completed audit
    for invalid in ("not JSON", '{"rules": [], "owner_words": []}'):  # a per-rule audit is not this audit
        start.audit.write_text(invalid)
        gated(transcript, "Bash", {"command": "true"}, comms=team_bus, binding=team_binding).judge()
        output = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert str(start.audit) in output["permissionDecisionReason"]
    start.prepare(DeckReader(), AuditDue(words), StartedDeal())
    gated(transcript, "Bash", {"command": "true"}, comms=team_bus, binding=team_binding).judge()
    assert json.dumps(Owed.nudges[0])[1:-1] in capsys.readouterr().out


def test_the_audit_answers_each_shown_prompt_by_its_own_words_with_an_action():
    due = AuditDue(("fix the gate", "land the queue"))
    duplicated = SessionAudit((OwnerWordsAnswer("fix the gate", "editing read_gate"),) * 2)
    assert "land the queue" in due.refusal(duplicated)
    idle = SessionAudit((OwnerWordsAnswer("fix the gate", "editing read_gate"),
                                  OwnerWordsAnswer("land the queue", "  ")))
    assert "no action" in due.refusal(idle)
    words = (OwnerWordsAnswer("fix the gate", "editing read_gate"),
             OwnerWordsAnswer("land the queue", "merging #3572"))
    assert "probe" in due.refusal(SessionAudit(words))  # the probes are owed too
    probes = tuple(FieldCodec.decode(Probe, answer) for answer in probe_answers(due.owner_words))
    assert due.refusal(SessionAudit(words, probes=probes)) == ""


def test_a_subagent_whose_start_event_never_came_is_declared_by_its_first_call(
        tmp_path, capsys, team_session, team_bus, team_binding):
    """Claude Code raises no SubagentStart for some agent types: a web-fetch subagent's first
    event was its WebFetch call, refused with "Thread 'claude-abfbd354543ff17a7' is not
    registered" (2026-11-08). Its first event declares the fork under the bound parent."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    assert DeckStart.decode(hook_record(transcript, "SessionStart")).take(team_bus, team_binding)
    assert "claude-b5" not in team_session.registry
    assert not refused(capsys, gated(transcript, "Bash", {"command": "ls"}, agent_id="b5",
                                     comms=team_bus, binding=team_binding))
    child = team_session.registry.require_member("claude-b5", ForkThread)
    assert tuple(child.parent.names()) == ("claude-code",)


def test_a_subagent_and_an_unbound_session_are_never_gated(
        tmp_path, monkeypatch, capsys, team_session, team_bus, team_binding):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    assert DeckStart.decode(hook_record(transcript, "SessionStart")).take(team_bus, team_binding)
    # Claude's SubagentStart registers the child before PreToolUse reports its activity.

    team_session.registry.register_child(
        "claude-a9", "claude-code", ForkThread, team_session.goals)
    assert not refused(capsys, gated(transcript, "Bash", {"command": "ls"}, agent_id="a9", comms=team_bus, binding=team_binding))
    elsewhere = tmp_path / "other.jsonl"
    elsewhere.write_text("")
    assert not refused(capsys, gated(elsewhere, "Bash", {"command": "ls"}, comms=team_bus, binding=team_binding))


def test_two_agents_on_one_session_keep_separate_deals(
        tmp_path, capsys, team_session, team_bus, team_binding):
    """A subagent shares its session's id and transcript path; its reads never count for the
    main agent, it may not write the main agent's audit, and its own deal is its own."""

    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    assert DeckStart.decode(hook_record(transcript, "SessionStart")).take(team_bus, team_binding)
    team_session.registry.register_child(
        "claude-a9", "claude-code", ForkThread, team_session.goals)
    for part in dealt_sources(transcript):
        gated(transcript, "Read", {"file_path": str(part)}, agent_id="a9", comms=team_bus, binding=team_binding).completed()
    assert refused(capsys, gated(transcript, "Bash", {"command": "ls"}, comms=team_bus, binding=team_binding))  # main still unread
    main_audit = transcript.with_suffix(".audit.json")
    assert refused(capsys, gated(transcript, "Write", {"file_path": str(main_audit)},
                                 agent_id="a9", comms=team_bus, binding=team_binding))
    sub = gated(transcript, "Bash", {"command": "true"}, agent_id="a9", comms=team_bus, binding=team_binding)
    assert sub.audit != main_audit and sub.audit.parent == transcript.with_suffix("") / "subagents"
    assert not refused(capsys, sub)  # the subagent was dealt nothing of its own
    assert not refused(capsys, gated(transcript, "Write", {"file_path": str(sub.audit)},
                                     agent_id="a9", comms=team_bus, binding=team_binding))


@pytest.mark.parametrize("retired", [False, True])
def test_a_resume_keeps_the_deal_its_session_is_answering(tmp_path, team_session, retired, team_bus, team_binding):
    """A resume keeps the context it had: the reads made and the audit written stay, where a
    re-deal on 2026-10-30 unlinked claude-code's audit mid-gate and demanded 23 reads again."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    assert session_start(transcript, "compact", comms=team_bus, binding=team_binding).returncode == 0
    start = DeckStart.decode(hook_record(transcript, "SessionStart"))
    first = start.current().paths[0]
    start.record_read(first, ReadSpan(1, sys.maxsize, sys.maxsize))
    start.audit.write_text('{"owner_words": [], "probes": [{"probe": "flinch", "answer": "No."}]}',
                           encoding="utf-8")
    if retired:
        old = start.current()
        start.publish(replace(old, dealt=(first.with_name("retired-source.md"), *old.dealt[1:])))
    result = session_start(transcript, "resume", comms=team_bus, binding=team_binding)
    assert result.returncode == 0, result.stderr
    assert fresh_confession(start) is retired
    assert start.current().read_by(start) == (() if retired else (first,))
    assert session_start(transcript, "compact", comms=team_bus, binding=team_binding).returncode == 0
    assert fresh_confession(start)  # a compaction owes a fresh confession, never a carried one
    assert start.current().read_by(start) == ()


def test_a_subagent_compaction_deals_the_subagent_not_its_session(tmp_path, team_session, team_bus, team_binding):
    """A subagent's events name its session's transcript; on 2026-10-30 a teammate fork's
    compaction dealt the main agent's stem, whose audit was mid-answer."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    assert session_start(transcript, "compact", comms=team_bus, binding=team_binding).returncode == 0
    main = DeckStart.decode(hook_record(transcript, "SessionStart"))
    main.audit.write_text('{"owner_words": []}', encoding="utf-8")
    dealt = main.record.read_bytes()
    result = session_start(transcript, "compact", agent_id="a9", comms=team_bus, binding=team_binding)
    assert result.returncode == 0, result.stderr
    assert main.audit.exists() and main.record.read_bytes() == dealt
    sub = DeckStart.of({**hook_record(transcript, "SessionStart"), "agent_id": "a9"})
    assert sub.record.exists() and str(sub.audit) in result.stdout


def test_a_compaction_deal_opens_with_when_it_compacted(tmp_path, team_session, team_bus, team_binding):
    """A fresh context cannot remember compacting, so the deal says when, from the transcript's
    own compact boundary: claude-code called real compactions a gate bug four times."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(json.dumps({
        "type": "system", "subtype": "compact_boundary", "content": "Conversation compacted",
        "uuid": "0460bd61-a862-449d-8421-4452811929fa", "timestamp": "2026-10-30T17:29:04.391Z",
    }) + "\n")
    start = DeckStart.decode({**hook_record(transcript, "SessionStart"), "source": "compact"})
    said = start.take(team_bus, team_binding)
    at = datetime(2026, 10, 30, 17, 29, 4, 391000, UTC).astimezone().isoformat(timespec="seconds")
    assert said[0].startswith(CompactedDeal(at).reason())
    assert start.current().trigger == CompactedDeal(at)


def test_turns_that_changed_nothing_are_held_up_against_what_the_session_wrote(
        tmp_path, capsys, team_bus, team_binding):
    """A yes opens the deal; the proof comes at the turn's end, with the session's own endings
    and what it wrote read back as Tristan asking, once per stop."""
    record = {"uuid": "00000000-0000-4000-8000-000000000001", "timestamp": "2026-10-31T04:00:00.000Z", "cwd": "/w", "version": "1"}
    ended = json.dumps({**record, "type": "system", "subtype": "turn_duration", "durationMs": 1})
    said = lambda words: json.dumps({**record, "type": "assistant", "message": {
        "role": "assistant", "content": [{"type": "text", "text": words}]}})
    called = lambda tool: json.dumps({**record, "type": "assistant", "message": {
        "role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": tool, "input": {}}]}})
    result = json.dumps({**record, "type": "user", "promptId": "p", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": [{"type": "text", "text": "r"}]}]}})
    transcript = tmp_path / "session.jsonl"
    transcript.with_suffix(".audit.json").write_text(json.dumps({"owner_words": [], "probes": [
        {"probe": "flinch", "answer": "I clear his board first."}]}))

    def stopped(*lines, active=False):
        transcript.write_text("\n".join(lines) + "\n")
        StopHook.decode({"hook_event_name": "Stop", "session_id": "s", "transcript_path": str(transcript),
                         "stop_hook_active": active}, comms=team_bus, binding=team_binding).guard()
        return capsys.readouterr().out

    idle = (said("Skipped"), ended) * 3
    asked = json.loads(stopped(*idle))["reason"]
    assert asked == "> I clear his board first.\n\ndid you mean it?"  # a quote and the question, no narration
    assert stopped(*idle, active=True) == ""  # never loops
    assert stopped(said("Skipped"), ended, said("Skipped"), ended) == ""  # two is not a pattern
    assert stopped(said("Skipped"), ended, called("Bash"), result, said("done"), ended,
                   said("Skipped"), ended, said("Skipped"), ended) == ""  # a call ran


def test_rows_waiting_on_a_stopped_thread_leave_the_main_view():
    """2026-11-04: the Pi units are stopped until Nov 9; their rows filled NEW and STILL OPEN."""
    from agent_comms.task_attachments.team_pings import AlarmGroup, NewAlarms, ParkedAlarms

    since = RecordedEventTime(1.0)
    worker = TeammateSubject(RecordedThreadIncarnation("worker", 1.0))
    live = RulePing(SpinningTeammate, TextDigest.of("live"), text="Read claude P1",
                    subjects=(worker,), since=since, threads=("claude",))
    parked = RulePing(SpinningTeammate, TextDigest.of("parked"), text="Read pi P2",
                      subjects=(worker,), since=since, threads=("pi-stopped",))
    claude = RecordedThreadIncarnation("claude", 1.0)
    packet = TeamDecisions(claude, (live, parked), (live, parked), ())
    sections = AlarmGroup.sections((packet,), (live, parked), frozenset({"pi-stopped"}))
    groups = {section.group: section for section in sections}
    assert groups[NewAlarms].pings == (live,)
    assert groups[ParkedAlarms].pings == (parked,)
    assert groups[ParkedAlarms].text_lines(2.0) == ()
    assert not ParkedAlarms.needs_disposal()
    assert parked.parked(frozenset({"pi-stopped"})) and not live.parked(frozenset({"pi-stopped"}))
