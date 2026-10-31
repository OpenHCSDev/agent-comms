"""Resident Stop evaluates actual recorded idle units, not a shell's invented answer."""

import io
import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import JsonValue

from agent_comms.child_process import ProcessIdentity
from agent_comms.cli_bridges.claude import ClaudeBridge
from agent_comms.cli_bridges.hook_deferral import FutureOrder, Later, NextTick, TranscriptRecord
from agent_comms.cli_bridges.hook_requests import HookReply, StopRequest
from agent_comms.cli_bridges.session_watch import WatchClaim
from agent_comms.comms import Comms
from agent_comms.repo import Checkout
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.threads import Thread
from agent_comms.transcript_events import AssistantTranscript
from agent_comms.turn_item import NativeItemId, NativeTurnId
from agent_comms_hooks import deferral
from hook_session_fixture import resident_hook, resident_session


def assistant(*blocks: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {"type": "assistant", "message": {"content": list(blocks)}}


def text(words: str) -> dict[str, JsonValue]:
    return {"type": "text", "text": words}


TOOL: dict[str, JsonValue] = {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}
RESULT: dict[str, JsonValue] = {
    "type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]}}

DEFERRING = [RESULT, assistant(text("The six failing tests have no owner. They go to a unit next."))]


@pytest.fixture
def stop_binding(durable_home: Path, commander_bus: Comms, tmp_path: Path) -> Iterator[ClaudeBridge]:
    commander = commander_bus.registry.snapshot().commander_project.require().commander
    with resident_session(commander_bus, tmp_path / "state", commander.name, "s") as bridge:
        yield bridge


def stop(bridge: ClaudeBridge, tmp_path: Path, records: list[dict[str, JsonValue]], *,
         idle: tuple[str, ...] = (), active: bool = False) -> HookReply:
    """The real idle owner reads registered running units and their retained turns."""
    for name in idle:
        bridge.comms.registry.declare(Thread(
            name, frozenset(), Checkout(tmp_path), execution=ExternalThreadExecution,
            process_identity=ProcessIdentity.capture(os.getpid())))
        bridge.comms.transcripts.external(name).finish(
            turn_id=NativeTurnId(name), item_id=NativeItemId("idle"), events=(AssistantTranscript(
                "Ready for work", position=NativeItemId("idle"), timestamp=time.time()),))
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
    return resident_hook(bridge, StopRequest, transcript, stop_hook_active=active)


def test_deferral_while_a_unit_is_idle_blocks_with_the_phrase_and_units(stop_binding, tmp_path):
    result = stop(stop_binding, tmp_path, DEFERRING, idle=("pi-story-tool", "unit-hygiene"))
    assert result.status == 0, result.stderr
    decision = json.loads(result.stdout)
    assert decision["decision"] == "block"
    assert "go to a unit next" in decision["reason"]
    assert "pi-story-tool, unit-hygiene" in decision["reason"]


def test_deferral_with_no_idle_unit_passes(stop_binding, tmp_path):
    result = stop(stop_binding, tmp_path, DEFERRING)
    assert (result.status, result.stdout) == (0, "")


def test_narration_before_a_tool_call_is_not_the_final_reply(stop_binding, tmp_path):
    records = [assistant(text("I'll order that later."), TOOL), RESULT,
               assistant(text("Ordered P362 to pi-story-tool."))]
    result = stop(stop_binding, tmp_path, records, idle=("pi-story-tool",))
    assert (result.status, result.stdout) == (0, "")


def test_a_missing_bound_transcript_is_not_a_normal_hook_reply(
        stop_binding: ClaudeBridge, tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        resident_hook(stop_binding, StopRequest, tmp_path / "missing.jsonl", stop_hook_active=False)


@pytest.mark.parametrize(("record", "said", "ends_reply"), [
    (assistant(text("Final reply")), "Final reply", False),
    (assistant(text("Narration"), TOOL), "Narration", True),
    (RESULT, "", True),
    ({"type": "unknown"}, "", False),
    ({"type": ["unknown"]}, "", False),
    (assistant({"type": "unknown"}), "", False),
    (assistant({"type": "user"}), "", False),
    ({"type": "text", "text": "A block is not a transcript record"}, "", False),
])
def test_transcript_members_and_unknowns_keep_their_reply_behavior(
        record: dict[str, JsonValue], said: str, ends_reply: bool):
    answer = TranscriptRecord.of(record)
    assert answer.said() == said
    assert answer.ends_reply is ends_reply


def test_a_stop_already_sent_back_passes(stop_binding, tmp_path):
    result = stop(stop_binding, tmp_path, DEFERRING, idle=("pi-story-tool",), active=True)
    assert (result.status, result.stdout) == (0, "")


@pytest.mark.parametrize(("shape", "reply"), [
    (NextTick, "They go to a unit next."),
    (Later, "I will look at it later."),
    (FutureOrder, "I'll assign it once this lands."),
])
def test_each_deferral_shape_recognises_its_phrase(shape, reply):
    assert tuple(shape.found(reply))


def test_a_bound_unit_session_stops_freely(durable_home: Path, commander_bus: Comms, tmp_path: Path):
    with resident_session(commander_bus, tmp_path / "state", "unit-x", "s") as bridge:
        result = stop(bridge, tmp_path, DEFERRING, idle=("pi-story-tool",))
        assert (result.status, result.stdout) == (0, "")


def test_an_unbound_session_stops_freely(
        durable_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "session_id": "unbound", "hook_event_name": "Stop",
        "transcript_path": str(tmp_path / "session.jsonl"), "stop_hook_active": False})))
    deferral.DeferralGuard.main()
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("actor", ("claude-code", "unit-x"))
def test_a_stop_with_no_monitor_on_a_running_bridge_is_sent_back_once(
        durable_home: Path, commander_bus: Comms, tmp_path: Path, actor: str):
    """Every bound role keeps its Monitor; only deferral judging is commander-specific."""
    quiet = [RESULT, assistant(text("Ordered P362 to pi-story-tool."))]
    with resident_session(commander_bus, tmp_path / "state", actor, "s") as bridge:
        claim = WatchClaim(bridge.binding.thread)
        assert stop(bridge, tmp_path, quiet).stdout == ""
        with claim.claim.claimed():
            decision = json.loads(stop(bridge, tmp_path, quiet).stdout)
            assert decision["decision"] == "block"
            assert "RE-ARM this session's watch" in decision["reason"]
            again = stop(bridge, tmp_path, quiet, active=True)
            assert (again.status, again.stdout) == (0, "")
            with claim.reader_held():
                held = stop(bridge, tmp_path, quiet)
                assert (held.status, held.stdout) == (0, "")
