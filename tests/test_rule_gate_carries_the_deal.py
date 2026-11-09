"""A cloud session has no launcher to pass --append-system-prompt-file, so its setup puts the deal
in the system prompt as an output style. Tristan, 2026-11-04: "any way to have the remote sessions
get the sytemprompt appneded ratherhta nrereading manually"."""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_comms.installer.deal_prompt import CloudDealPrompt, DealPrompt
from agent_comms.team.package import TeamPackage
from agent_comms_hooks.skill import DeckReader

GATE = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "rule_gate.py"
SESSION = "6fb47df8-855a-4719-ae63-a9b5a7c3dd23"


def run(home: Path, *verb: str, stdin: str = "") -> str:
    done = subprocess.run(
        [sys.executable, str(GATE), *verb], input=stdin, capture_output=True, text=True, check=True,
        timeout=60, env={**os.environ, "HOME": str(home)})
    return done.stdout


def hook(home: Path, verb: str, **record: object) -> str:
    return run(home, verb, stdin=json.dumps({"session_id": SESSION, **record}))


def manifest(home: Path) -> Path:
    return home / ".claude" / "state" / "rule-deal-carried.json"


@pytest.fixture
def carried(tmp_path: Path) -> Path:
    (tmp_path / ".claude" / "state").mkdir(parents=True)
    run(tmp_path, "carry")
    return tmp_path


def test_the_style_holds_the_whole_deal_and_keeps_the_coding_instructions(carried: Path):
    style = (carried / ".claude" / "output-styles" / "team-deal.md").read_text(encoding="utf-8")
    assert style.startswith("---\nname: team-deal\n") and "keep-coding-instructions: true\n---\n" in style
    for source in DeckReader.sources():
        assert source.read_text(encoding="utf-8").strip() in style


def test_it_promises_no_gate_a_cloud_session_does_not_have():
    assert "the gate refuses every" in DealPrompt.preamble
    assert "the gate refuses every" not in CloudDealPrompt.preamble
    assert CloudDealPrompt.render().reads == DealPrompt.render().reads


def test_a_session_carrying_the_deal_owes_no_reading_after_a_start_or_a_compaction(carried: Path):
    for source in ("startup", "compact"):
        told = hook(carried, "deal", source=source)
        assert "in your system prompt" in told and "Before ANY other tool call" not in told
        assert hook(carried, "gate", tool_name="Bash", tool_input={"command": "true"}) == ""


def test_only_a_file_changed_since_setup_is_read_again(carried: Path):
    changed = TeamPackage.rules_files()[0].resolve()
    digests = json.loads(manifest(carried).read_text())
    assert digests[str(changed)] == hashlib.sha256(changed.read_bytes()).hexdigest()
    digests[str(changed)] = "changed since setup"
    manifest(carried).write_text(json.dumps(digests))

    told = hook(carried, "deal", source="startup")
    assert str(changed) in told
    assert str(TeamPackage.rules_files()[1].resolve()) not in told
    assert "Rule reading owed: 1 of 1" in hook(carried, "gate", tool_name="Bash", tool_input={})
    assert hook(carried, "gate", tool_name="Read", tool_input={"file_path": str(changed)}) == ""


def test_a_session_without_setup_still_reads_everything(tmp_path: Path):
    assert "Before ANY other tool call" in hook(tmp_path, "deal", source="startup")
    assert "Rule reading owed" in hook(tmp_path, "gate", tool_name="Bash", tool_input={})


def test_a_session_whose_start_hook_never_ran_is_dealt_on_its_first_call(tmp_path: Path):
    """A real cloud session showed no SessionStart output and no deck while PreToolUse ran."""
    (tmp_path / ".claude" / "state").mkdir(parents=True)
    assert "Rule reading owed" in hook(tmp_path, "gate", tool_name="Bash", tool_input={})
    assert (tmp_path / ".claude" / "state" / f"rule-dealt-{SESSION}").is_file()


def test_a_session_is_told_how_to_connect_on_either_way_it_can_start(carried: Path):
    """A cloud snapshot freezes the system prompt for days, so this is told fresh at each deal."""
    assert "agent-comms-inbox-watch URL" in hook(carried, "deal", source="startup")
    first_call = hook(carried, "gate", session_id="other", tool_name="Bash", tool_input={})
    assert "agent-comms-inbox-watch URL" in json.loads(first_call)["hookSpecificOutput"]["additionalContext"]


def test_a_hook_runs_under_the_checkouts_python_with_its_input(tmp_path: Path):
    """A cloud sandbox's python3 is older than the code the gate imports."""
    checkout = tmp_path / "checkout"
    (checkout / ".claude" / "hooks").mkdir(parents=True)
    (checkout / ".claude" / "hooks" / "rule_gate.py").write_text(GATE.read_text())
    python = checkout / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/sh\necho "ran under the checkout: $2"; cat\n')
    python.chmod(0o755)
    done = subprocess.run([sys.executable, str(checkout / ".claude" / "hooks" / "rule_gate.py"), "gate"],
                          input="the record", capture_output=True, text=True, check=True, timeout=30)
    assert done.stdout == "ran under the checkout: gate\nthe record"


def test_a_carrying_session_is_dealt_on_its_first_call_and_still_owes_nothing(carried: Path):
    assert "permissionDecision" not in hook(carried, "gate", tool_name="Bash", tool_input={})
    assert (carried / ".claude" / "state" / f"rule-dealt-{SESSION}").is_file()


def test_a_finished_reading_is_not_dealt_again_by_the_gate(carried: Path):
    hook(carried, "deal", source="startup")
    digests = json.loads(manifest(carried).read_text())
    digests[str(TeamPackage.rules_files()[0].resolve())] = "changed after the deal"
    manifest(carried).write_text(json.dumps(digests))
    assert hook(carried, "gate", tool_name="Bash", tool_input={}) == ""


def test_install_selects_the_style_and_keeps_the_settings_it_found(tmp_path: Path, monkeypatch):
    spec = importlib.util.spec_from_file_location("rule_gate", GATE)
    gate = importlib.util.module_from_spec(spec)
    sys.modules["rule_gate"] = gate
    spec.loader.exec_module(gate)
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"model": "kept"}))
    monkeypatch.setenv("HOME", str(tmp_path))
    ran = []
    monkeypatch.setattr(gate.subprocess, "run", lambda command, **_: ran.append(command))

    gate.install()

    document = json.loads(settings.read_text())
    assert document["outputStyle"] == "team-deal" and document["model"] == "kept"
    assert document["permissions"]["defaultMode"] == "auto"
    assert ran[-1] == [str(gate.checkout_python()), str(GATE.resolve()), "carry"]


def test_a_failing_hook_allows_the_call_and_says_so(tmp_path: Path):
    said = json.loads(run(tmp_path, "gate", stdin="not json"))
    assert "rule_gate gate failed and allowed this call" in said["systemMessage"]
