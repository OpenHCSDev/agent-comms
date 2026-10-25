"""The real SessionStart command works before a commander connects to the bus."""


from agent_comms.repo import Checkout
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_comms.cli_bridges.claude import BoardLineRow
from agent_comms.cli_bridges.shared import Binding
from agent_comms.commander.package import CommanderPackage
from agent_comms.commander.rule_file import RuleFile
from agent_comms.comms import wire
from agent_comms.pattern_sources import PostmortemFailure
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.registry_document import RegisteredCommanderProject
from agent_comms.slop import Slop
from agent_comms.task_attachments.commander_pings import (
    CommanderDecisions,
    CommanderNotice,
    RulePing,
    UnitPing,
)
from agent_comms.text_digest import TextDigest
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.thread_model import DeclaredModel
from agent_comms.threads import Thread
from agent_comms.ui_model.unit_state import AwaitingApplicationRun, DeliveredUnclosed
from agent_comms_hooks.board import BoardLine
from agent_comms_hooks.reread import BoardReminder, DeafReminder, SliceReminder
from agent_comms_hooks.skill import CommanderStart
from agent_comms_hooks.watch import DeafWatch, WatchClaim, WatchRecord


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


def test_the_board_rides_every_tool_call_until_a_packet_clears_it(tmp_path, monkeypatch):
    """The bridge records the packet in its delivery store; the hook prints the lines the
    commander disposes of on every call, and a packet without them clears the board."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    hook = BoardReminder.decode(hook_record(tmp_path / "session.jsonl"))
    claim = WatchClaim(hook.session_id)
    record = WatchRecord("lead", str(tmp_path / "bus"), ("watch",), tmp_path / "inbox")
    lead = RecordedThreadIncarnation("lead", 1.0)
    # 30 s clear of the next minute so both calls render alike.
    began = RecordedEventTime(time.time() - 90)
    stale = RulePing(DeliveredUnclosed, TextDigest.of("CLOSE"), text="CLOSE unit-a P1 -> close it", since=began)
    waiting = UnitPing(RecordedThreadIncarnation("worker", 1.0), AwaitingApplicationRun, (), (),
                       TextDigest.of("AWAIT"), text="AWAIT RUN worker", since=began)
    with claim.held(record), claim.reader_held(), Binding.open(record.store.parent) as db:
        assert hook.take() == ()  # the bridge has recorded no packet
        BoardLineRow.record(db, CommanderDecisions(lead, (stale, waiting), (stale, waiting), ()))
        line, = stale.board(BoardLine)
        assert hook.take() == hook.take() == ("BOARD: CLOSE unit-a P1 -> close it (seen for 1 min)",)
        BoardLineRow.record(db, CommanderNotice(lead, (waiting,)))
        assert hook.take() == ()


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
            name, frozenset(), Checkout.from_wire(str(tmp_path)),
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
