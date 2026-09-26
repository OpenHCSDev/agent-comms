"""Proof-journal startup failures are explicit, bounded, and never replay inputs."""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent_comms import backend
from agent_comms.diagnostics import FailureReason, record_terminal_failure


@contextmanager
def refuse_native_send(*_args):
    yield False  # The tests must never submit a prompt or invoke a provider.


def _pi_stub(tmp_path: Path, script: str) -> str:
    executable = tmp_path / "pi-native"
    executable.write_text(f"#!{sys.executable}\n" + script)
    executable.chmod(0o755)
    return str(executable)


@pytest.mark.asyncio
async def test_complete_near_limit_journal_warns_before_native_prompt(tmp_path):
    session = tmp_path / "owner.jsonl"
    session.write_text("")
    proof = Path(f"{session}.input-proof")
    with proof.open("wb") as stream:
        stream.truncate(backend._NATIVE_PROOF_JOURNAL_WARN_BYTES)
    stub = _pi_stub(
        tmp_path,
        """import json, sys
request = json.loads(sys.stdin.readline())
assert request["type"] == "get_state"
print(json.dumps({"type":"response", "command":"get_state", "id":request["id"],
                  "success":True, "data":{"nativeInputProofCapability":
                  "pi-native-input-v1-live-only"}}), flush=True)
assert sys.stdin.readline() == ""  # refused at send_boundary before any prompt
""",
    )
    events = [
        event
        async for event in backend.stream_agent_events(
            stub,
            [],
            "never send this prompt",
            str(tmp_path),
            session_file=str(session),
            send_boundary=refuse_native_send,
        )
    ]
    assert [event["type"] for event in events].count("notice") == 1
    assert "approaching its 128 MiB startup limit" in next(
        event["text"] for event in events if event["type"] == "notice"
    )
    assert events[-1]["ok"] is False
    assert events[-1]["diagnostic"].get("reason") != FailureReason.PROOF_JOURNAL_LIMIT
    assert proof.stat().st_size == backend._NATIVE_PROOF_JOURNAL_WARN_BYTES


@pytest.mark.asyncio
async def test_over_limit_preflight_exit_has_safe_explicit_diagnostic(tmp_path):
    session = tmp_path / "owner.jsonl"
    session.write_text("")
    proof = Path(f"{session}.input-proof")
    with proof.open("wb") as stream:
        stream.truncate(backend._NATIVE_PROOF_JOURNAL_LIMIT_BYTES + 1)
    stub = _pi_stub(
        tmp_path,
        """import sys
print('Error: Truncated or oversized native input proof journal', file=sys.stderr)
sys.exit(1)
""",
    )
    events = [
        event
        async for event in backend.stream_agent_events(
            stub, [], "never send this prompt", str(tmp_path), session_file=str(session)
        )
    ]
    terminal = events[-1]
    assert terminal["ok"] is False
    assert terminal["diagnostic"]["reason"] == FailureReason.PROOF_JOURNAL_LIMIT
    assert terminal["diagnostic"]["proof_journal_bytes"] == proof.stat().st_size
    assert "exceeded its 128 MiB startup limit before this prompt was sent" in terminal["text"]
    assert "Truncated or oversized" not in terminal["text"]
    record = record_terminal_failure(
        tmp_path, turn_id="a" * 32, thread="owner", event=terminal, sequences=()
    )
    persisted = json.loads(record.read_text())
    assert persisted["reason"] == FailureReason.PROOF_JOURNAL_LIMIT
    assert persisted["measurements"]["proof_journal_bytes"] == proof.stat().st_size
    assert proof.stat().st_size == backend._NATIVE_PROOF_JOURNAL_LIMIT_BYTES + 1


@pytest.mark.asyncio
async def test_foreign_preflight_exit_is_not_misclassified(tmp_path):
    session = tmp_path / "owner.jsonl"
    session.write_text("")
    Path(f"{session}.input-proof").write_text("")
    stub = _pi_stub(tmp_path, "import sys\nprint('unrelated', file=sys.stderr)\nsys.exit(1)\n")
    events = [
        event
        async for event in backend.stream_agent_events(
            stub, [], "never send this prompt", str(tmp_path), session_file=str(session)
        )
    ]
    assert events[-1]["diagnostic"]["reason"] == FailureReason.PREFLIGHT_EXIT
    assert "proof journal" not in events[-1]["text"]
