"""The real SessionStart command works before a commander connects to the bus."""


from agent_comms.repo import Checkout
import inspect
import io
import json
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.cli_bridges.monitor import BoardReply, ReadBoard, SessionRefused, SessionRequest
from agent_comms.cli_bridges.shared import Binding, DeliveredBoardContent
from agent_comms.heartbeat_subjects import UnitSubject
from agent_comms.channel_targets import CommandersTag
from agent_comms.commander.package import CommanderPackage
from agent_comms.commander.rule_file import CommanderAudience, RuleFile
from agent_comms.commander.standing import PlanIdle
from agent_comms.goal_actions import CreateProblemGoalAction
from commander_heartbeat_fixture import capture_frame
from agent_comms.plan_phases import BusWriterPhase
from agent_comms.repository_name import RepositoryName
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec, OrderId
from agent_comms.pattern_sources import PostmortemFailure, ReadThenRepeatedFailure
from agent_comms.registry_document import RegisteredCommanderProject
from agent_comms.slop import Slop
from agent_comms.commander.spinning import SpinningUnit
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.standing_rules import OrderlessUnreviewedHead, ReviewTimerUp
from agent_comms.text_digest import TextDigest
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.task_attachments.commander_pings import CommanderDecisions, CommanderUpdate, RulePing
from agent_comms.thread_model import DeclaredModel
from agent_comms.threads import Thread
from agent_comms.ui_model.unit_state import AwaitingCommander
from agent_comms.todos import ProgressRead, ReviewedHeadVerdict
from agent_comms_hooks.board import SessionBoard, UnexpectedBoardReply
from agent_comms_hooks.reread import BoardReminder, DeafReminder, SliceReminder
from agent_comms_hooks.deferral import StopHook
from agent_comms_hooks.forks import SubagentHook
from agent_comms.rule_deal import AuditDue, OwnerWordsAnswer, RuleAudit, SessionAudit
from agent_comms_hooks.read_gate import GatedCall
from agent_comms_hooks.skill import CommanderReader, DeckReader, DeckStart, FleetReader
from agent_comms_hooks.watch import DeafWatch, WatchClaim, WatchRecord
from test_commander_notifications import beat, lead_hud
from test_head_review import ACCEPTED, invoke

pytest_plugins = ("test_head_review", "test_commander_notifications")


def session_start(transcript, source):
    return subprocess.run(
        [sys.executable, "-m", "agent_comms_hooks.skill"],
        input=json.dumps({"hook_event_name": "SessionStart", "source": source, "session_id": "s",
                          "transcript_path": str(transcript)}),
        capture_output=True, text=True, timeout=30,
    )


@pytest.mark.parametrize("source", ["startup", "resume", "compact"])
def test_an_unbound_session_start_gets_no_commander_sources(tmp_path, monkeypatch, source):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    result = session_start(transcript, source)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert not transcript.with_suffix(".deal.json").exists()


@pytest.mark.parametrize("source", ["startup", "resume", "compact"])
def test_the_commander_session_start_reads_all_package_files(tmp_path, source, commanding_session):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    result = session_start(transcript, source)
    assert result.returncode == 0, result.stderr
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("Commander: claude-code.")
    assert "WHAT TRISTAN ACTUALLY SAID LAST" in context
    assert "FUCK YOUR COMPACTION" in context
    dealt = dealt_sources(transcript)
    assert all(str(path) in context for path in dealt)
    expected = (
        *CommanderPackage.paper(),
        CommanderPackage.voice(),
        CommanderPackage.indictment(),
        Path(inspect.getfile(PostmortemFailure)),
        CommanderPackage.skill(),
        *(rule.path for rule in RuleFile.packaged()),
        Slop.catalog() / "README.md",
    )
    assert len(dealt) == len(expected) and set(dealt) == {path.resolve() for path in expected}


def dealt_sources(transcript):
    """The files the last deal named, read from its one record."""
    return DeckStart.decode(hook_record(transcript, "SessionStart")).current().sources()


def hook_record(transcript, event="PostToolUse"):
    return {"hook_event_name": event, "session_id": "s", "transcript_path": str(transcript),
            "tool_name": "Bash", "tool_input": {}}


def test_no_deck_no_slices_outside_a_commander_session(tmp_path):
    hook = SliceReminder.decode(hook_record(tmp_path / "session.jsonl"))
    assert [hook.take() for _ in range(SliceReminder.every * 2)] == [()] * SliceReminder.every * 2
    assert not hook.calls.exists()


def test_a_slice_every_few_calls_rotating_and_dealt_again_at_compaction(tmp_path, commanding_session):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    start = DeckStart.decode(hook_record(transcript, "SessionStart"))
    assert start.take()
    hook = SliceReminder.decode(hook_record(transcript))
    slices = CommanderReader.slices()
    taken = [hook.take() for _ in range(SliceReminder.every * 3)]
    assert taken[SliceReminder.every - 1::SliceReminder.every] == [(text,) for text in slices[:3]]
    assert sum(map(len, taken)) == 3
    assert start.take()  # compaction deals the deck again from its start
    assert [hook.take() for _ in range(SliceReminder.every)][-1] == (slices[0],)


def test_rearm_rides_every_tool_call_while_a_running_bridge_has_no_monitor(tmp_path, commanding_session):
    hook = DeafReminder.decode(hook_record(tmp_path / "session.jsonl"))
    claim = WatchClaim(hook.session_id)
    deaf = DeafWatch("now (no Monitor holds its reader claim)").line()
    assert deaf not in hook.take()  # the bound bridge is not running: nothing to re-arm
    with claim.claim.claimed():
        assert deaf in hook.take() and deaf in hook.take()
        with claim.reader_held():
            assert deaf not in hook.take()


def test_an_unbound_session_is_never_told_to_rearm_a_commander_watch(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    hook = DeafReminder.decode(hook_record(tmp_path / "session.jsonl"))
    assert [hook.take() for _ in range(SliceReminder.every * 2)] == [()] * SliceReminder.every * 2
    assert not hook.deck(CommanderReader()).exists()
    assert not hook.deck(FleetReader()).exists()


def tool_call(hook, server, comms, db, now):
    """One tool call's board lines: the hook asks over the session socket while this thread
    answers as the bridge does, deriving the board from Core at the moment of asking. The
    commander's deck slices ride the same call and are not the board."""
    with ThreadPoolExecutor(1) as pool:
        taken = pool.submit(hook.take)
        peer, _ = server.accept()
        with peer, peer.makefile("rwb") as stream:
            assert FieldCodec.decode(SessionRequest, FieldCodec.parse(stream.readline())) == ReadBoard()
            entries = CommanderUpdate.current_board(comms, "lead")
            reply = BoardReply(DeliveredBoardContent.deliver(db, entries, now).lines)
            stream.write(FieldCodec.compact(FieldCodec.encode(reply)).encode() + b"\n")
        return tuple(line for line in taken.result(timeout=5) if line.startswith("BOARD"))


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
    record = WatchRecord("lead", str(comms.root), ("watch",), tmp_path / "inbox")
    worker = RecordedThreadIncarnation("worker", 1.0)
    rules = (*OrderlessUnreviewedHead.scan_reviews(comms.goals, "lead", ProgressRead(())),
             ReviewTimerUp("P1", worker, "order-P1", RecordedEventTime(1000.0)),
             SpinningUnit("P2", worker, 92))
    beat(comms, rules)
    beat(comms, rules)
    with (claim.held(record), claim.reader_held(), Binding.open(tmp_path / "delivery") as db,
          socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server):
        server.bind(str(claim.socket_path))
        server.listen()
        server.settimeout(5)
        assert DeckStart.decode(hook_record(tmp_path / "session.jsonl", "SessionStart")).take()
        board = tool_call(hook, server, comms, db, time.time())
        assert len(board) == len(rules)
        invoke(comms, request, ReviewedHeadVerdict(ACCEPTED))
        assert [line for line in tool_call(hook, server, comms, db, time.time())
                if f"--pr {request.pr} --head {request.head}" in line] == []


def test_board_decoder_names_a_refusal_instead_of_a_constructor_error():
    refused = SessionRefused(error="The bridge cannot serve this request")
    reply = json.loads(FieldCodec.compact(FieldCodec.encode(refused)))
    with pytest.raises(UnexpectedBoardReply) as caught:
        SessionBoard.replied(**reply)
    assert refused.error in str(caught.value)


def test_board_read_failure_is_a_typed_hook_failure(tmp_path):
    with pytest.raises(UnexpectedBoardReply) as caught:
        SessionBoard.asked(tmp_path / "missing.socket")
    assert caught.value.__cause__ is not None


def test_board_start_line_disappears_when_its_phase_order_is_sent(notification_bus, tmp_path, monkeypatch):
    comms = notification_bus
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", "home")
    hook = BoardReminder.decode(hook_record(tmp_path / "session.jsonl"))
    claim = WatchClaim(hook.session_id)
    claim.path.parent.mkdir(parents=True)
    record = WatchRecord("lead", str(comms.root), ("watch",), tmp_path / "inbox")
    reading = capture_frame(comms, "lead", ProgressRead()).plan
    beat(comms, (PlanIdle(BusWriterPhase, reading),))
    phase_name = f"{BusWriterPhase.plan.declared_name}/{BusWriterPhase.declared_name}"
    lead = comms.registry.require("lead").incarnation
    request = comms.goals.todos.add_request(lead, "Start the ready bus writer phase").id
    original = comms.goals.todos.get("P1").order.checked()
    with (claim.held(record), claim.reader_held(), Binding.open(tmp_path / "delivery") as db,
          socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server):
        server.bind(str(claim.socket_path))
        server.listen()
        server.settimeout(5)
        assert DeckStart.decode(hook_record(tmp_path / "session.jsonl", "SessionStart")).take()
        before = tool_call(hook, server, comms, db, time.time())
        assert sum(phase_name in line for line in before) == 1
        # The worker holds P1 and P2 already; the idle outsider takes the phase order.
        comms.goals.update_goal("outsider", CreateProblemGoalAction(
            problem="P3", commander="lead", request=request, assigner=lead,
            repo=RepositoryName.from_wire("OpenHCSDev/agent-comms"),
            text="Start the ready bus writer phase", review_at=RecordedEventTime(time.time() + 3600),
            order=replace(original, id=OrderId.mint(), serves=BusWriterPhase,
                          at=RecordedEventTime(time.time())),
        ))
        after = tool_call(hook, server, comms, db, time.time())
        assert sum(phase_name in line for line in after) == 0
        assert not after


def test_the_board_prints_changes_once_then_an_unchanged_count(notification_bus, tmp_path, monkeypatch):
    """Real packets, bridge delivery journal and session socket; no hook-side state."""
    comms = notification_bus
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", "home")
    hook = BoardReminder.decode(hook_record(tmp_path / "session.jsonl"))
    claim = WatchClaim(hook.session_id)
    claim.path.parent.mkdir(parents=True)
    record = WatchRecord("lead", str(comms.root), ("watch",), tmp_path / "inbox")
    lead = comms.registry.require("lead").incarnation
    worker = comms.registry.require("worker").incarnation
    began = time.time()
    first = RulePing(SpinningUnit, TextDigest.of("first"), text="Read worker P1",
                     subjects=(UnitSubject(worker),), since=RecordedEventTime(began))
    second = RulePing(ReviewTimerUp, TextDigest.of("second"), text="Review worker P2",
                      subjects=(UnitSubject(worker),), since=RecordedEventTime(began))
    hud = lead_hud(comms).hud_text(began + 90)
    CommanderDecisions(lead, (first, second), (first, second), ()).send(comms, began + 90, hud)
    with (claim.held(record), claim.reader_held(), Binding.open(tmp_path / "delivery") as db,
          socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server):
        server.bind(str(claim.socket_path))
        server.listen()
        server.settimeout(5)
        assert DeckStart.decode(hook_record(tmp_path / "session.jsonl", "SessionStart")).take()
        assert tool_call(hook, server, comms, db, began + 90) == (
            "BOARD: Read worker P1 (seen for 1 min)",
            "BOARD: Review worker P2 (seen for 1 min)",
        )
        assert tool_call(hook, server, comms, db, began + 91) == (
            "BOARD: 2 unchanged (oldest: Read worker P1, seen 1 min)",
        )
        changed = replace(first, observation=TextDigest.of("changed"), since=RecordedEventTime(began + 90))
        CommanderDecisions(lead, (changed, second), (changed,), ()).send(comms, began + 92, hud)
        assert tool_call(hook, server, comms, db, began + 92) == (
            "BOARD: Read worker P1 (seen for 0 min)",
            "BOARD: 1 unchanged (oldest: Review worker P2, seen 1 min)",
        )
        # A fresh packet does not reset delivery, and the answer window rings only once.
        CommanderDecisions(lead, (changed, second), (), ()).send(comms, began + 93, hud)
        alarm = began + AwaitingCommander.seconds
        assert tool_call(hook, server, comms, db, alarm + 1) == (
            "BOARD: Review worker P2 (seen for 5 min)",
            "BOARD: 1 unchanged (oldest: Read worker P1, seen 3 min)",
        )
        assert tool_call(hook, server, comms, db, alarm + 2) == (
            "BOARD: 2 unchanged (oldest: Review worker P2, seen 5 min)",
        )
        assert DeliveredBoardContent.count(db) == 2
        CommanderDecisions(lead, (), (), ()).send(comms, alarm + 3, hud)
        assert tool_call(hook, server, comms, db, alarm + 3) == ()
        assert DeliveredBoardContent.count(db) == 0


def test_the_deck_carries_the_voice_every_fourth_slice_every_rule_reminder_and_every_failure():
    slices = CommanderReader.slices()
    voice = DeckReader.paragraphs(CommanderPackage.voice().read_text())
    indictment = DeckReader.paragraphs(CommanderPackage.indictment().read_text())
    assert set(slices[0::4]) == set(voice)
    assert set(slices[1::4]) == set(indictment)
    assert set(slices[2::4]) == {rule.reminder for rule in RuleFile.packaged()}
    assert set(slices[3::4]) == {
        f"Recorded failure {line}" for line in PostmortemFailure.catalog().splitlines()}
    assert all(len(text) <= DeckReader.slice_chars for text in slices)


def run_installed(monkeypatch, capsys, transcript):
    from agent_comms_hooks import reread, skill

    def run(program, event):
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(hook_record(transcript, event))))
        {"PostToolUse": reread, "SessionStart": skill}[event].main()
        return capsys.readouterr().out

    return run


def test_the_installed_commands_order_the_reads_once_then_put_slices_inline(
        tmp_path, monkeypatch, capsys, commanding_session):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    run = run_installed(monkeypatch, capsys, transcript)
    assert run(None, "SessionStart")
    outputs = [run(None, "PostToolUse") for _ in range(SliceReminder.every)]
    assert outputs[:-1] == [""] * (SliceReminder.every - 1)
    context = json.loads(outputs[-1])["hookSpecificOutput"]["additionalContext"]
    assert context == CommanderReader.slices()[0]
    assert "Read each of these" not in context


def test_a_session_that_becomes_commander_is_dealt_its_sources_on_its_next_tool_call(
        tmp_path, monkeypatch, capsys, commander_bus):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    run = run_installed(monkeypatch, capsys, transcript)
    assert not run(None, "SessionStart")
    assert not run(None, "PostToolUse")
    WatchClaim("s").enrol(WatchRecord("claude-code", str(commander_bus.root), (), tmp_path / "state"))
    assert "Read each of these" in run(None, "PostToolUse")
    assert transcript.with_suffix(".deck.json").exists()


def test_a_unit_session_gets_the_fleet_deck_and_no_commander_reminders(
        tmp_path, monkeypatch, capsys, commander_bus):
    """A bound session that is not commander follows the fleet rules: it is ordered to read
    them, its summary is called a paraphrase, and its slices come from the fleet deck."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    WatchClaim("s").enrol(WatchRecord("unit-x", str(commander_bus.root), (), tmp_path / "state"))
    run = run_installed(monkeypatch, capsys, transcript)
    context = json.loads(run(None, "SessionStart"))["hookSpecificOutput"]["additionalContext"]
    assert "Read each of these" in context and "Your summary is a paraphrase" in context
    assert "WHAT TRISTAN ACTUALLY SAID LAST" in context
    assert "What your units actually hold" not in context
    outputs = [run(None, "PostToolUse") for _ in range(SliceReminder.every)]
    assert outputs[:-1] == [""] * (SliceReminder.every - 1)
    context = json.loads(outputs[-1])["hookSpecificOutput"]["additionalContext"]
    assert context == FleetReader.slices()[0]
    assert transcript.with_suffix(".fleet-deck.json").exists()
    assert not transcript.with_suffix(".deck.json").exists()


@pytest.mark.parametrize("path", CommanderPackage.rules_files(), ids=lambda path: path.name)
def test_package_rule_opens_with_its_wakeup_then_its_reason(path):
    rule = RuleFile.read(path)
    assert rule.why and rule.wakeup.startswith(">")
    assert rule.render().startswith(rule.wakeup + "\n\n" + rule.summary)
    assert rule.reminder.startswith(rule.wakeup) and rule.reminder.endswith(rule.drift)


def test_rule_file_without_a_wakeup_is_refused(tmp_path):
    source = CommanderPackage.rules_files()[0].read_text(encoding="utf-8")
    path = tmp_path / "asleep.md"
    path.write_text("".join(line for line in source.splitlines(keepends=True)
                            if not line.startswith(">")), encoding="utf-8")
    with pytest.raises(ValueError, match="wake-up"):
        RuleFile.read(path)


def test_package_descriptions_do_not_repeat_rule_kind():
    for path in CommanderPackage.rules_files():
        metadata, _ = RuleFile.document(path)
        assert not metadata["description"].startswith("HARD RULE:"), path.name


def test_failure_catalog_numbers_every_evidenced_member_once():
    members = sorted(PostmortemFailure.members_with(PostmortemFailure), key=lambda member: member.number)
    assert [member.number for member in members] == list(range(1, len(members) + 1))
    assert all(member.evidence for member in members)




def test_a_session_bound_to_another_bus_thread_reads_the_fleet_rules_and_not_the_commanders(
        tmp_path, monkeypatch, commander_bus):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    record = {"hook_event_name": "SessionStart", "session_id": "s", "transcript_path": str(transcript)}
    WatchClaim("s").enrol(WatchRecord("unit-x", str(commander_bus.root), (), tmp_path))
    start = DeckStart.decode(record)
    assert start.take()
    dealt = start.current().sources()
    assert all(rule.path.resolve() in dealt for rule in FleetReader.rules())
    commander_only = [rule for rule in RuleFile.packaged() if rule.audience is CommanderAudience]
    assert commander_only and not any(rule.path.resolve() in dealt for rule in commander_only)
    assert CommanderPackage.indictment().resolve() not in dealt


def test_the_commander_reads_every_packaged_rule():
    assert CommanderReader.rules() == RuleFile.packaged()


def test_the_reading_order_names_exactly_the_packaged_rules_and_the_spine_leads():
    names = tuple(RuleFile.read(path).name for path in CommanderPackage.rules_files())
    assert sorted(RuleFile.reading_order) == sorted(names)
    assert tuple(rule.name for rule in RuleFile.packaged()) == RuleFile.reading_order
    assert RuleFile.reading_order[0] == "spine"


def test_every_deal_reads_the_paper_before_any_rule():
    for reader in (FleetReader, CommanderReader):
        sources = reader.sources()
        paper = CommanderPackage.paper()
        assert paper and sources[:len(paper)] == paper
        assert sources[len(paper)] == RuleFile.packaged()[0].path


def test_the_commander_session_gets_its_sources(tmp_path, monkeypatch, commander_bus):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    WatchClaim("s").enrol(WatchRecord("claude-code", str(commander_bus.root), (), tmp_path))
    assert DeckStart.decode(
        {"hook_event_name": "SessionStart", "session_id": "s", "transcript_path": str(transcript)}).take()


def test_a_demoted_commander_keeps_its_deck_files_but_they_get_no_vote(
        tmp_path, capsys, commanding_session):
    """Command moves to another thread in the registry; the old commander's dealt deck stays
    on disk, and no hook treats it as command: no reminder, stop guard or fork."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    assert DeckStart.decode(hook_record(transcript, "SessionStart")).take()
    assert transcript.with_suffix(".deck.json").exists()
    commanding_session.registry.set_commander_project(RegisteredCommanderProject(
        commanding_session.registry.require("unit-x").incarnation, ()))
    reminder = BoardReminder.decode(hook_record(transcript))
    (order, _, header) = reminder.take()  # now a fleet session: dealt the fleet deck, once
    assert "Read each of these" in order and "What your units actually hold" not in header
    assert [reminder.take() for _ in range(SliceReminder.every - 1)] == [()] * (SliceReminder.every - 1)
    assert transcript.with_suffix(".fleet-deck.json").exists()
    StopHook.decode({"hook_event_name": "Stop", "session_id": "s",
                     "transcript_path": str(transcript), "stop_hook_active": False}).guard()
    assert capsys.readouterr().out == ""
    SubagentHook.decode({"hook_event_name": "SubagentStart", "session_id": "s",
                         "transcript_path": str(transcript), "agent_id": "a9",
                         "agent_type": "Explore"}).register()
    assert "slot claude-code-explore" in capsys.readouterr().out  # a fleet slot, not a fork
    assert "claude-a9" not in commanding_session.registry
    commanding_session.registry.require("claude-code-explore").execution.require_subagent()
    assert transcript.with_suffix(".deck.json").exists()


def test_a_commander_fork_registers_under_the_session_that_spawned_it(
        tmp_path, capsys, commanding_session):
    """No CLI subprocess: the hook declares the fork in its own process as a child of the
    session's own bus thread, carrying every tag that thread carries (#commanders included),
    even when that thread is a co-commander and not the registry's lead."""
    commanding_session.registry.declare(Thread(
        "claude-nra", frozenset({CommandersTag.declared_name}), Checkout(tmp_path),
        model=DeclaredModel("anthropic/claude-opus-4-6")))
    WatchClaim("s2").enrol(WatchRecord(
        "claude-nra", str(commanding_session.root), (), tmp_path / "state2"))
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    for session, agent in (("s", "a7"), ("s2", "a8")):
        SubagentHook.decode({"hook_event_name": "SubagentStart", "session_id": session,
                             "transcript_path": str(transcript), "agent_id": agent,
                             "agent_type": "fork"}).register()
        assert f"commander fork claude-{agent}" in capsys.readouterr().out
    snapshot = commanding_session.registry.snapshot()
    for agent, parent in (("a7", "claude-code"), ("a8", "claude-nra")):
        fork = snapshot.require(f"claude-{agent}")
        fork.execution.require_commander_fork()
        assert tuple(fork.parent.names()) == (parent,)
        assert fork.tags == snapshot.require(parent).tags
    assert CommandersTag.declared_name in snapshot.require("claude-a8").tags
    assert {"claude-a7", "claude-a8"}.isdisjoint(
        thread.name for thread in snapshot.commander_project.commanders(snapshot))


def gated(transcript, tool, tool_input, **extra):
    return GatedCall.of({"hook_event_name": "PreToolUse", "session_id": "s",
                         "transcript_path": str(transcript), "tool_name": tool,
                         "tool_input": tool_input, **extra})


def refused(capsys, call):
    call.judge()
    out = capsys.readouterr().out
    return out and json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_every_tool_but_read_waits_for_each_dealt_rule_file_then_its_audit(
        tmp_path, capsys, commanding_session):
    """The deal names the packaged rule files themselves; PreToolUse only judges, and a file
    counts as read only when a Read covering all of it completes (PostToolUse). Then only the
    audit may be written until it answers every dealt rule."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    assert DeckStart.decode(hook_record(transcript, "SessionStart")).take()
    parts = list(dealt_sources(transcript))
    assert set(parts) == {source.resolve() for source in CommanderReader.sources()}
    assert refused(capsys, gated(transcript, "Bash", {"command": "ls"}))
    whole = {"file_path": str(parts[0])}
    assert not refused(capsys, gated(transcript, "Read", whole))
    assert refused(capsys, gated(transcript, "Read", {"file_path": str(transcript)}))  # not dealt
    assert refused(capsys, gated(transcript, "Edit", {}))  # a Read that never completed
    gated(transcript, "Read", {**whole, "limit": 5}).completed()
    assert refused(capsys, gated(transcript, "Edit", {}))  # a partial read is not a reading
    for part in parts:
        gated(transcript, "Read", {"file_path": str(part)}).completed()
    audit = transcript.with_suffix(".audit.json")
    assert refused(capsys, gated(transcript, "Bash", {"command": "ls"}))  # no audit yet
    assert not refused(capsys, gated(transcript, "Write", {"file_path": str(audit)}))
    audit.write_text(json.dumps({"rules": [{"rule": rule.name} for rule in CommanderReader.rules()],
                                 "owner_words": []}))
    assert not refused(capsys, gated(transcript, "Bash", {"command": "ls"}))
    assert DeckStart.decode(hook_record(transcript, "SessionStart")).take()  # compaction
    assert refused(capsys, gated(transcript, "Bash", {"command": "ls"}))


def test_refusal_counts_names_tool_and_prefills_the_owned_audit(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    start = DeckStart.decode(hook_record(transcript, "SessionStart"))
    words = ("Read the rules.\nThen write the audit.",)
    start.prepare(CommanderReader(), words)
    for attempt, tool in enumerate(("Bash", "Edit"), 1):
        gated(transcript, tool, {}).judge()
        output = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        reason = output["permissionDecisionReason"]
        assert f"You just tried {tool} before reading. Call #{attempt} since this compaction, refused." in reason
        assert f"Tristan told you, verbatim:\n{words[0]}\nYou did not." in reason
        assert ReadThenRepeatedFailure().cite() in reason
        assert ReadThenRepeatedFailure.title in reason
    for source in CommanderReader.sources():
        gated(transcript, "Read", {"file_path": str(source)}).completed()
    gated(transcript, "Bash", {}).judge()
    reason = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecisionReason"]
    template = FieldCodec.decode(SessionAudit, json.loads(reason[reason.index("{\n"):]))
    due = start.current().due
    assert {entry.rule for entry in template.rules} == due.rules
    assert tuple(answer.words for answer in template.owner_words) == words
    assert all(not answer.doing for answer in template.owner_words)
    assert due.refusal(template)  # the blank template cannot count as the completed audit
    for invalid in ("not JSON", '{"rules": [], "owner_words": []}'):
        start.audit.write_text(invalid)
        gated(transcript, "Bash", {}).judge()
        output = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        reason = output["permissionDecisionReason"]
        assert FieldCodec.decode(SessionAudit, json.loads(reason[reason.index("{\n"):])) == template
    start.prepare(CommanderReader(), words)
    gated(transcript, "Bash", {}).judge()
    assert "Call #1 since this compaction, refused." in capsys.readouterr().out


def test_the_audit_answers_each_shown_prompt_by_its_own_words_with_an_action():
    due = AuditDue(frozenset({"voice"}), ("fix the gate", "land the queue"))
    audited = (RuleAudit("voice"),)
    duplicated = SessionAudit(audited, (OwnerWordsAnswer("fix the gate", "editing read_gate"),) * 2)
    assert "land the queue" in due.refusal(duplicated)
    idle = SessionAudit(audited, (OwnerWordsAnswer("fix the gate", "editing read_gate"),
                                  OwnerWordsAnswer("land the queue", "  ")))
    assert "no action" in due.refusal(idle)
    answered = SessionAudit(audited, (OwnerWordsAnswer("fix the gate", "editing read_gate"),
                                      OwnerWordsAnswer("land the queue", "merging #3572")))
    assert due.refusal(answered) == ""
    assert "voice" in due.refusal(SessionAudit((), answered.owner_words))


def test_a_subagent_and_an_unbound_session_are_never_gated(
        tmp_path, monkeypatch, capsys, commanding_session):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    assert DeckStart.decode(hook_record(transcript, "SessionStart")).take()
    # Claude's SubagentStart registers the child before PreToolUse reports its activity.
    from agent_comms.thread_execution import CommanderForkExecution

    commanding_session.registry.register_child(
        "claude-a9", "claude-code", CommanderForkExecution, commanding_session.goals)
    assert not refused(capsys, gated(transcript, "Bash", {"command": "ls"}, agent_id="a9"))
    elsewhere = tmp_path / "other.jsonl"
    elsewhere.write_text("")
    assert not refused(capsys, gated(elsewhere, "Bash", {"command": "ls"}))


def test_two_agents_on_one_session_keep_separate_deals(
        tmp_path, capsys, commanding_session):
    """A subagent shares its session's id and transcript path; its reads never count for the
    main agent, it may not write the main agent's audit, and its own deal is its own."""
    from agent_comms.thread_execution import CommanderForkExecution

    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    assert DeckStart.decode(hook_record(transcript, "SessionStart")).take()
    commanding_session.registry.register_child(
        "claude-a9", "claude-code", CommanderForkExecution, commanding_session.goals)
    for part in dealt_sources(transcript):
        gated(transcript, "Read", {"file_path": str(part)}, agent_id="a9").completed()
    assert refused(capsys, gated(transcript, "Bash", {"command": "ls"}))  # main still unread
    main_audit = transcript.with_suffix(".audit.json")
    assert refused(capsys, gated(transcript, "Write", {"file_path": str(main_audit)},
                                 agent_id="a9"))
    sub = gated(transcript, "Bash", {}, agent_id="a9")
    assert sub.audit != main_audit and sub.audit.parent == transcript.with_suffix("") / "subagents"
    assert not refused(capsys, sub)  # the subagent was dealt nothing of its own
    assert not refused(capsys, gated(transcript, "Write", {"file_path": str(sub.audit)},
                                     agent_id="a9"))
