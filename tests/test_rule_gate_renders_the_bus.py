"""A cloud session's reader sees only the session's own text, so a turn that took a bus message
cannot end until that text names it (talk.md). On 2026-11-06 Tristan, reading
claude-i-notices in the Claude app, did not see his own message 64189 that the session had been
shown."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "rule_gate.py"
SESSION = "6fb47df8-855a-4719-ae63-a9b5a7c3dd23"
EVENT = ("<task-notification>\n<task-id>b307menn5</task-id>\n"
         '<summary>Monitor event: "claude-i-notices inbox watch"</summary>\n'
         "<event>Comms in · user (human) · 64189-2165ab5d3be9\n"
         "Here I'm sending another message from the bus on my phone.</event>\n"
         "</task-notification>")
TIME = "2026-11-06T02:06:00.000Z"


def record(kind: str, number: int, **fields: object) -> str:
    """A cloud session's record by default: Claude Code stamps each with its entrypoint."""
    return json.dumps({"type": kind, "uuid": f"00000000-0000-4000-8000-{number:012d}",
                       "parentUuid": None, "timestamp": TIME, "sessionId": SESSION,
                       "entrypoint": "remote", **fields})


def queued(number: int) -> str:
    """The shape a watch event takes when it lands while a turn is running."""
    return record("attachment", number, attachment={
        "type": "queued_command", "prompt": EVENT, "origin": {"kind": "task-notification"}})


def opening(number: int) -> str:
    """The shape it takes when it starts a turn of its own."""
    return record("user", number, promptId="11111111-1111-4111-8111-111111111111",
                  origin={"kind": "task-notification"},
                  message={"role": "user", "content": EVENT})


def said(number: int, text: str) -> str:
    return record("assistant", number, message={
        "id": f"msg_{number}", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1}})


def ended() -> str:
    return json.dumps({"type": "system", "subtype": "stop_hook_summary", "hasOutput": False,
                       "uuid": "00000000-0000-4000-8000-999999999999", "timestamp": TIME})


SYSTEM_PYTHON = shutil.which("python3", path="/usr/bin:/usr/local/bin:/bin")


def stop(tmp_path: Path, *lines: str, interpreter: str = sys.executable, **event: object) -> str:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("".join(line + "\n" for line in lines))
    done = subprocess.run(
        [interpreter, str(GATE), "idle"],
        input=json.dumps({"session_id": SESSION, "transcript_path": str(transcript), **event}),
        capture_output=True, text=True, check=True, timeout=60, env={"HOME": str(tmp_path)})
    return done.stdout


@pytest.mark.parametrize("arrival", [queued, opening])
def test_a_bus_message_the_text_never_named_holds_the_stop(tmp_path: Path, arrival):
    refused = json.loads(stop(tmp_path, arrival(1), said(2, "Nothing for me there.")))
    assert refused["decision"] == "block"
    assert "64189-2165ab5d3be9" in refused["reason"]


@pytest.mark.parametrize("arrival", [queued, opening])
def test_a_bus_message_named_in_the_text_lets_the_stop_through(tmp_path: Path, arrival):
    assert stop(tmp_path, arrival(1), said(2, "> Tristan via the bus, 02:06 UTC (64189): …")) == ""


def test_a_message_an_ended_turn_already_answered_for_holds_nothing(tmp_path: Path):
    assert stop(tmp_path, queued(1), said(2, "skipped it"), ended(), said(3, "next turn")) == ""


@pytest.mark.skipif(SYSTEM_PYTHON is None, reason="no system python3 on this machine")
def test_settings_system_python_still_decodes_under_the_checkout_venv(tmp_path: Path):
    """settings.json runs `python3 rule_gate.py idle`; main re-execs it under the checkout's
    venv, which alone has the decoder's dependencies (claude-f-ci on #4174, 2026-11-06)."""
    refused = json.loads(stop(tmp_path, queued(1), said(2, "skipped"), interpreter=SYSTEM_PYTHON))
    assert "64189-2165ab5d3be9" in refused["reason"]


def test_a_host_session_read_in_toad_stops_freely(tmp_path: Path):
    """On the host Tristan reads the bus in Toad, so the bridge's Monitor events owe no copy
    (claude-a-toad on #4174, 2026-11-06)."""
    host = json.loads(queued(1))
    host["entrypoint"] = "cli"
    assert stop(tmp_path, json.dumps(host), said(2, "Nothing for me there.")) == ""


def test_the_refusal_lets_the_next_stop_through(tmp_path: Path):
    assert stop(tmp_path, queued(1), stop_hook_active=True) == ""
