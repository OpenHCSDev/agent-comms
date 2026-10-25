"""The real SessionStart command works before a commander connects to the bus."""


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
from agent_comms.commander.package import CommanderPackage
from agent_comms.commander.rule_file import RuleFile
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.pattern_sources import PostmortemFailure
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
from agent_comms_hooks.board import SessionBoard, UnexpectedBoardReply, UnreadableBoard
from agent_comms_hooks.reread import BoardReminder, DeafReminder, SliceReminder
from agent_comms_hooks.skill import CommanderStart
from agent_comms_hooks.watch import DeafWatch, WatchClaim, WatchRecord
from test_commander_notifications import beat, lead_hud
from test_head_review import ACCEPTED, invoke

pytest_plugins = ("test_head_review", "test_commander_notifications")


@pytest.mark.parametrize("source", ["startup", "resume", "compact"])
def test_session_start_reads_all_package_files_without_a_bus(tmp_path, source):
    home = tmp_path / "home"
    home.mkdir()
    bus = tmp_path / "bus"
    bus.mkdir()
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    result = subprocess.run(
        [sys.executable, "-m", "agent_comms_hooks.skill"],
        input=json.dumps(
            {"hook_event_name": "SessionStart", "source": source, "session_id": "fresh-session",
             "transcript_path": str(transcript)}
        ),
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert len(context) < 10_000
    parts = [Path(line) for line in context.splitlines()[1:]]
    assert parts == sorted(transcript.with_suffix(".reread").iterdir())
    text = "".join(part.read_text() for part in parts)
    expected_notice = "No commander project is registered for this session."
    assert text.startswith(expected_notice)
    assert text.split("\n\n", 1)[1] == "\n\n".join((
        CommanderPackage.voice().read_text(),
        CommanderPackage.indictment().read_text(),
        "# Recorded failures\n\n" + PostmortemFailure.catalog(),
        RuleFile.document(CommanderPackage.skill())[1],
        *(rule.render() for rule in RuleFile.packaged()),
        (Slop.catalog() / "README.md").read_text(),
    ))
    assert not tuple(bus.iterdir())


def hook_record(transcript, event="PostToolUse"):
    return {"hook_event_name": event, "session_id": "s", "transcript_path": str(transcript),
            "tool_name": "Bash", "tool_input": {}}


def test_no_deck_no_slices_outside_a_commander_session(tmp_path):
    hook = SliceReminder.decode(hook_record(tmp_path / "session.jsonl"))
    assert [hook.take() for _ in range(SliceReminder.every * 2)] == [()] * SliceReminder.every * 2
    assert not hook.calls.exists()


def test_a_slice_every_few_calls_rotating_and_dealt_again_at_compaction(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    start = CommanderStart.decode(hook_record(transcript, "SessionStart"))
    assert start.take()
    hook = SliceReminder.decode(hook_record(transcript))
    slices = CommanderStart.slices()
    taken = [hook.take() for _ in range(SliceReminder.every * 3)]
    assert taken[SliceReminder.every - 1::SliceReminder.every] == [(text,) for text in slices[:3]]
    assert sum(map(len, taken)) == 3
    assert start.take()  # compaction deals the deck again from its start
    assert [hook.take() for _ in range(SliceReminder.every)][-1] == (slices[0],)


def test_rearm_rides_every_tool_call_while_a_running_bridge_has_no_monitor(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    hook = DeafReminder.decode(hook_record(tmp_path / "session.jsonl"))
    claim = WatchClaim(hook.session_id)
    claim.path.parent.mkdir(parents=True)
    assert hook.take() == ()  # no bridge was ever bound: nothing to re-arm
    with claim.claim.claimed():
        deaf = DeafWatch("now (no Monitor holds its reader claim)").line()
        assert hook.take() == hook.take() == (deaf,)
        with claim.reader_held():
            assert hook.take() == ()


def tool_call(hook, server, comms, db, now):
    """One tool call's board: the hook asks over the session socket while this thread answers
    as the bridge does, deriving the board from Core at the moment of asking."""
    with ThreadPoolExecutor(1) as pool:
        taken = pool.submit(hook.take)
        peer, _ = server.accept()
        with peer, peer.makefile("rwb") as stream:
            assert FieldCodec.decode(SessionRequest, FieldCodec.parse(stream.readline())) == ReadBoard()
            entries = CommanderUpdate.current_board(comms, "lead")
            reply = BoardReply(DeliveredBoardContent.deliver(db, entries, now).lines)
            stream.write(FieldCodec.compact(FieldCodec.encode(reply)).encode() + b"\n")
        return taken.result(timeout=5)


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
    assert refused.error in UnreadableBoard(caught.value).lines[0]


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
    hud = lead_hud(comms)
    CommanderDecisions(lead, (first, second), (first, second), ()).send(comms, began + 90, hud)
    with (claim.held(record), claim.reader_held(), Binding.open(tmp_path / "delivery") as db,
          socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server):
        server.bind(str(claim.socket_path))
        server.listen()
        server.settimeout(5)
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


def test_the_deck_carries_the_voice_every_fourth_slice_every_drift_tell_and_every_failure():
    slices = CommanderStart.slices()
    voice = CommanderStart.paragraphs(CommanderPackage.voice().read_text())
    indictment = CommanderStart.paragraphs(CommanderPackage.indictment().read_text())
    assert set(slices[0::4]) == set(voice)
    assert set(slices[1::4]) == set(indictment)
    assert set(slices[2::4]) == set(RuleFile.drift_tells())
    assert set(slices[3::4]) == {
        f"Recorded failure {line}" for line in PostmortemFailure.catalog().splitlines()}
    assert all(len(text) < 2_000 for text in slices)


def test_the_installed_commands_order_the_reads_once_then_put_slices_inline(
        tmp_path, monkeypatch, capsys):
    import io

    from agent_comms_hooks import reread, skill

    def run(program, event):
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(hook_record(transcript, event))))
        program.main()
        return capsys.readouterr().out

    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert not run(reread, "PostToolUse")
    assert run(skill, "SessionStart")
    outputs = [run(reread, "PostToolUse") for _ in range(SliceReminder.every)]
    assert outputs[:-1] == [""] * (SliceReminder.every - 1)
    (document,) = outputs[-1].splitlines()
    context = json.loads(document)["hookSpecificOutput"]["additionalContext"]
    assert context == CommanderStart.slices()[0]
    assert "Read each of these" not in context


@pytest.mark.parametrize("path", CommanderPackage.rules_files(), ids=lambda path: path.name)
def test_package_rule_has_a_reason(path):
    assert RuleFile.read(path).why


def test_package_descriptions_do_not_repeat_rule_kind():
    for path in CommanderPackage.rules_files():
        metadata, _ = RuleFile.document(path)
        assert not metadata["description"].startswith("HARD RULE:"), path.name


def test_failure_catalog_numbers_every_evidenced_member_once():
    members = sorted(PostmortemFailure.members_with(PostmortemFailure), key=lambda member: member.number)
    assert [member.number for member in members] == list(range(1, len(members) + 1))
    assert all(member.evidence for member in members)




@pytest.fixture
def commander_bus(tmp_path):
    bus = wire(tmp_path / "bus")
    for name in ("claude-code", "unit-x"):
        bus.registry.declare(Thread(
            name, frozenset(), str(tmp_path),
            model=DeclaredModel("anthropic/claude-opus-4-6")))
    bus.registry.set_commander_project(RegisteredCommanderProject(
        bus.registry.require("claude-code").incarnation, ()))
    return bus


def test_a_session_bound_to_another_bus_thread_gets_no_commander_sources(
        tmp_path, monkeypatch, commander_bus):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    record = {"hook_event_name": "SessionStart", "session_id": "s", "transcript_path": str(transcript)}
    WatchClaim("s").enrol(WatchRecord("unit-x", str(commander_bus.root), (), tmp_path))
    assert CommanderStart.decode(record).take() == ()
    assert not CommanderStart.decode(record).parts.exists()


def test_the_commander_session_gets_its_sources(tmp_path, monkeypatch, commander_bus):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    WatchClaim("s").enrol(WatchRecord("claude-code", str(commander_bus.root), (), tmp_path))
    assert CommanderStart.decode(
        {"hook_event_name": "SessionStart", "session_id": "s", "transcript_path": str(transcript)}).take()
