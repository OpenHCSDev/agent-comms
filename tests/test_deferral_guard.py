"""The real Stop command sends back a commander reply that defers work while Core counts a unit idle."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_comms_hooks.deferral import FutureOrder, Later, NextTick


def assistant(*blocks):
    return {"type": "assistant", "message": {"content": list(blocks)}}


def text(words):
    return {"type": "text", "text": words}


TOOL = {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}
RESULT = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]}}


def stop(tmp_path: Path, records, *, idle: str, active: bool = False) -> subprocess.CompletedProcess:
    """Run the installed hook module with Core's idle command answered by `idle` (a shell body)."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
    transcript.with_suffix(".reread").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    command = bin_dir / "agent-comms-commander"
    command.write_text(f"#!/bin/sh\n{idle}\n")
    command.chmod(0o755)
    return subprocess.run(
        [sys.executable, "-m", "agent_comms_hooks.deferral"],
        input=json.dumps({"session_id": "s", "hook_event_name": "Stop",
                          "transcript_path": str(transcript), "stop_hook_active": active}),
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True, text=True, timeout=60,
    )


DEFERRING = [RESULT, assistant(text("The six failing tests have no owner. They go to a unit next."))]


def test_deferral_while_a_unit_is_idle_blocks_with_the_phrase_and_units(tmp_path):
    result = stop(tmp_path, DEFERRING, idle="printf 'pi-story-tool\\nunit-hygiene\\n'")
    assert result.returncode == 0, result.stderr
    decision = json.loads(result.stdout)
    assert decision["decision"] == "block"
    assert "go to a unit next" in decision["reason"]
    assert "pi-story-tool, unit-hygiene" in decision["reason"]


def test_deferral_with_no_idle_unit_passes(tmp_path):
    result = stop(tmp_path, DEFERRING, idle="exit 0")
    assert (result.returncode, result.stdout) == (0, "")


def test_narration_before_a_tool_call_is_not_the_final_reply(tmp_path):
    records = [assistant(text("I'll order that later."), TOOL), RESULT,
               assistant(text("Ordered P362 to pi-story-tool."))]
    result = stop(tmp_path, records, idle="echo pi-story-tool")
    assert (result.returncode, result.stdout) == (0, "")


def test_a_stop_already_sent_back_passes(tmp_path):
    result = stop(tmp_path, DEFERRING, idle="echo pi-story-tool", active=True)
    assert (result.returncode, result.stdout) == (0, "")


def test_a_failed_idle_read_blocks_and_says_so(tmp_path):
    result = stop(tmp_path, DEFERRING, idle="echo 'no commander configured' >&2; exit 3")
    decision = json.loads(result.stdout)
    assert decision["decision"] == "block"
    assert "no commander configured" in decision["reason"]


@pytest.mark.parametrize(("shape", "reply"), [
    (NextTick, "They go to a unit next."),
    (Later, "I will look at it later."),
    (FutureOrder, "I'll assign it once this lands."),
])
def test_each_deferral_shape_recognises_its_phrase(shape, reply):
    assert tuple(shape.found(reply))
