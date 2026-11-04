"""The checkout's reading gate and Core's gate never both hold one session: on 2026-11-04 a bound
session had each refusing the paths the other demanded, then, with its bridge down, Core allowing
only the connect call this gate refused."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_comms_hooks.watch import SessionEndpoint

GATE = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "rule_gate.py"
SESSION = "6fb47df8-855a-4719-ae63-a9b5a7c3dd23"
CONNECT = {"tool_name": "Bash", "tool_input": {"command": "agent-comms-claude-bridge connect"}}


def hook(home: Path, verb: str, **record: object) -> str:
    done = subprocess.run(
        [sys.executable, str(GATE), verb], input=json.dumps({"session_id": SESSION, **record}),
        capture_output=True, text=True, check=True, timeout=30, env={"HOME": str(home)})
    return done.stdout


def enroll(home: Path) -> None:
    record = SessionEndpoint(SESSION, home).record_path
    record.parent.mkdir(parents=True)
    record.write_text("{}")


@pytest.fixture
def dealt(tmp_path: Path) -> Path:
    """A session dealt the checkout's list and not yet bound, as every session starts."""
    assert "Before ANY other tool call" in hook(tmp_path, "deal", source="compact")
    return tmp_path


def test_an_unbound_session_owes_this_gate_its_reading(dealt: Path):
    assert "Rule reading owed" in hook(dealt, "gate", **CONNECT)


def test_a_session_bound_after_its_deal_answers_only_to_core(dealt: Path):
    enroll(dealt)
    assert hook(dealt, "gate", **CONNECT) == ""
    assert hook(dealt, "gate", tool_name="Read", tool_input={"file_path": "/runtime/spine.md"}) == ""


def test_a_bound_session_is_not_dealt_a_second_list(tmp_path: Path):
    enroll(tmp_path)
    assert hook(tmp_path, "deal", source="compact") == ""
    assert not (tmp_path / ".claude" / "state" / f"rule-gate-{SESSION}.json").exists()


@pytest.fixture
def running_job():
    """A real child of this test shaped like a Claude Code background shell, so the hook, a child
    of this test too, finds it among its parent's shells."""
    # The trailing command keeps bash from replacing itself with sleep, which drops the marker.
    job = subprocess.Popen(["bash", "-c", ": shell-snapshots/snapshot-job; sleep 30; :"])
    yield job
    job.kill()
    job.wait()


def test_a_running_job_refuses_the_first_stop_and_lets_the_next_through(tmp_path: Path, running_job):
    """2026-11-04: no limit on the refusal, so Tristan watched it fire eight times in one wait."""
    assert json.loads(hook(tmp_path, "idle"))["decision"] == "block"
    assert hook(tmp_path, "idle", stop_hook_active=True) == ""


def test_the_bus_watch_is_not_a_job_to_wait_for():
    """The watch stays armed for the session's whole life; counting it refused every stop."""
    spec = importlib.util.spec_from_file_location("rule_gate", GATE)
    gate = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = gate
    spec.loader.exec_module(gate)
    session, hook_process, shell = "100", "101", "source /x/shell-snapshots/snapshot-bash.sh && eval"
    watch = [["200", session, f"{shell} 'agent-comms-claude-bridge watch'"]]
    job = [["201", session, f"{shell} 'pytest tests'"]]
    tree = [[session, "1", "claude"], [hook_process, session, "python3 rule_gate.py idle"]]
    assert gate.jobs_among(tree + watch, int(hook_process)) == 0
    assert gate.jobs_among(tree + watch + job, int(hook_process)) == 1
