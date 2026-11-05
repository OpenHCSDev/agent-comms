"""A gate that refuses every tool cannot refuse the way out of itself."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_comms_hooks.hook_switch import HooksVerb, StartVerb, StopVerb
from agent_comms_hooks.shell_commands import EmergencyHookStop

GATE = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "rule_gate.py"
STOP = "agent-comms-hooks stop --from someone --why 'the gate is wrong'"


@pytest.mark.parametrize("command, passes", (
    (STOP, True),
    ("agent-comms-hooks stop --from a --why x=y", True),
    ("/home/ts/.local/bin/agent-comms-hooks stop --from a --why b", False),
    ("./agent-comms-hooks stop --from a --why b", False),
    ("/tmp/x/agent-comms-hooks stop --from a --why b", False),
    ("LD_PRELOAD=/tmp/x.so agent-comms-hooks stop --from a --why b", False),
    ("PYTHONPATH=/tmp/evil agent-comms-hooks stop --from a --why b", False),
    ("BASH_ENV=/tmp/x PATH=/tmp agent-comms-hooks stop --from a --why b", False),
    ("env A=1 agent-comms-hooks stop --from a --why b", False),
    ("$(echo agent-comms-hooks) stop --from a --why b", False),
    ("agent-comms-hooks stop --from a --why `id`", False),
    ("agent-comms-hooks stop --from a --why b > /tmp/x", False),
    (f"{STOP}; rm -rf /tmp/x", False),
    (f"{STOP} && echo smuggled", False),
    (f"echo smuggled; {STOP}", False),
    (f"{STOP} | tee /tmp/x", False),
    ("agent-comms-hooks start", False),
    ("echo agent-comms-hooks stop", False),
    ("ls", False),
))
def test_the_way_out_runs_only_as_the_whole_command(command, passes):
    assert EmergencyHookStop.is_whole_of(command) is passes


def gate(home: Path, verb: str, **record: object) -> str:
    return subprocess.run(
        [sys.executable, str(GATE), verb],
        input=json.dumps({"session_id": "unbound-session", **record}),
        capture_output=True, text=True, check=True, timeout=30, env={"HOME": str(home)}).stdout


def test_the_reading_gate_lets_the_stop_through_and_names_it(tmp_path):
    gate(tmp_path, "deal", source="compact")
    refused = gate(tmp_path, "gate", tool_name="Bash", tool_input={"command": "ls"})
    assert json.loads(refused)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert EmergencyHookStop.way_out in refused
    assert "deny" not in gate(tmp_path, "gate", tool_name="Bash", tool_input={"command": STOP})
    assert "deny" in gate(tmp_path, "gate", tool_name="Bash",
                          tool_input={"command": f"{STOP}; ls"})


def settings(home: Path) -> dict:
    return json.loads((home / ".claude/settings.json").read_text())


def test_stop_and_start_flip_only_the_one_setting(tmp_path, monkeypatch):
    path = tmp_path / ".claude/settings.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"hooks": {"PreToolUse": []}, "env": {"A": "1"}}))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0))
    assert "off" in StopVerb(sender="a", why="b").run(tmp_path)
    assert settings(tmp_path) == {"hooks": {"PreToolUse": []}, "env": {"A": "1"},
                                  HooksVerb.setting: True}
    assert "on" in StartVerb().run(tmp_path)
    assert settings(tmp_path) == {"hooks": {"PreToolUse": []}, "env": {"A": "1"}}


@pytest.mark.parametrize("failure", (
    lambda *a, **k: subprocess.CompletedProcess(a, 1, stderr="bus down"),
    lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("agent-comms", 30)),
))
def test_a_dead_bus_does_not_undo_the_stop(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(subprocess, "run", failure)
    report = StopVerb(sender="a", why="b").run(tmp_path)
    assert settings(tmp_path)[HooksVerb.setting] is True
    assert "failed" in report


@pytest.mark.parametrize("content", ('{"hooks": ', "[]"))
def test_a_broken_settings_file_is_named_and_left_alone(tmp_path, content):
    path = tmp_path / ".claude/settings.json"
    path.parent.mkdir()
    path.write_text(content)
    with pytest.raises(ValueError, match="fix or move it"):
        StopVerb(sender="a", why="b").run(tmp_path)
    assert path.read_text() == content


def test_the_post_cannot_outlast_the_foreground_cap():
    assert HooksVerb.post_seconds < 15


def test_the_verbs_parse_from_the_command_the_gate_passes():
    stop = HooksVerb.parse(["stop", "--from", "a", "--why", "b"])
    assert isinstance(stop, StopVerb) and stop.sender == "a" and stop.why == "b"
    assert isinstance(HooksVerb.parse(["start"]), StartVerb)
