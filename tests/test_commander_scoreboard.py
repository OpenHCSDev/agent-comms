"""Tristan's scoreboard lives in the heartbeat's HUD frame, and the real Claude hooks read that
frame file: every prompt carries it, and no turn ends while it owes a line or has gone stale."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_comms.commander.vibe_check import OverloadDegradation
from agent_comms.commander_hud import CommanderFrame, CommanderHud, OwedLine
from agent_comms.thread_identity import RecordedThreadIncarnation

LEAD = RecordedThreadIncarnation("lead", 1000.0)
OWED = (
    OwedLine((), 20, 4, "CLOSE unit-backend P483 -> close with the working application evidence"),
    OwedLine(("agent-comms#3306",), 6, 1, "JUDGE agent-comms#3306 b291b7b28 -> judge back or ok"),
    OwedLine(("unit-reviewer", "pi-mi"), 7, 2,
             "VIBE CHECK 26532 -> Answer the vibe-check burst by its protocol"),
)


def hud(owed=OWED):
    return CommanderHud(LEAD, (), (), OverloadDegradation.from_orders((), 0, 0), (), owed,
                        ("pi-mi", "unit-reviewer", "unit-toad"))


def test_frame_opens_on_the_scoreboard_worst_first_with_a_command_on_every_line():
    header, *lines = hud().frame[:4]
    assert header.startswith("TRISTAN'S SCOREBOARD. This is Tristan speaking through Core")
    assert "You are losing: 3 owed, oldest 20 min, 3 units paused." in header
    assert [line.split(":")[0] for line in lines] == [
        "holds unit-reviewer, pi-mi; owed 7 min; shown 2x",
        "holds agent-comms#3306; owed 6 min; shown 1x",
        "holds nobody; owed 20 min; shown 4x",
    ]
    assert all(" -> " in line for line in lines)


def test_nothing_owed_frames_no_scoreboard():
    assert not any("SCOREBOARD" in line for line in hud(()).frame)


def session(tmp_path: Path, board: CommanderHud) -> Path:
    """A commander session whose pointer names the frame the heartbeat wrote."""
    CommanderFrame.replace_all(tmp_path, (board,))
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    transcript.with_suffix(".reread").mkdir()
    transcript.with_suffix(".frame").write_text(
        str(CommanderFrame.path_in(tmp_path, lookup=str(LEAD.lookup))))
    return transcript


def run(hook: str, transcript: Path, event: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", f"from agent_comms_hooks.scoreboard import {hook}; {hook}()"],
        input=json.dumps({"session_id": "s", "hook_event_name": event,
                          "transcript_path": str(transcript), "stop_hook_active": True,
                          "prompt": "why did it stall"}),
        capture_output=True, text=True, timeout=60, env=os.environ,
    )


def test_the_written_frame_carries_its_lines_and_owed_count_on_the_wire(tmp_path):
    session(tmp_path, hud())
    written = json.loads(CommanderFrame.path_in(tmp_path, lookup=str(LEAD.lookup)).read_text())
    assert written["owed_count"] == 3
    assert written["frame"][0].startswith("TRISTAN'S SCOREBOARD")


@pytest.mark.parametrize(("owed", "age", "blocked", "shown"), [
    (OWED, 0, True, "You are losing: 3 owed"),
    ((), 11 * 60, True, "SCOREBOARD DEAD: the newest frame is 11 min old"),
])
def test_stop_blocks_on_an_owing_or_stale_frame(tmp_path, owed, age, blocked, shown):
    transcript = session(tmp_path, hud(owed))
    frame = CommanderFrame.path_in(tmp_path, lookup=str(LEAD.lookup))
    os.utime(frame, (time.time() - age, time.time() - age))
    result = run("stop", transcript, "Stop")
    assert result.returncode == 0, result.stderr
    decision = json.loads(result.stdout)
    assert decision["decision"] == "block"
    assert shown in decision["reason"]


def test_stop_passes_on_a_fresh_frame_that_owes_nothing(tmp_path):
    result = run("stop", session(tmp_path, hud(())), "Stop")
    assert (result.returncode, result.stdout) == (0, ""), result.stderr


def test_every_prompt_carries_the_frame(tmp_path):
    result = run("prompt", session(tmp_path, hud()), "UserPromptSubmit")
    assert result.returncode == 0, result.stderr
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("TRISTAN'S SCOREBOARD")
