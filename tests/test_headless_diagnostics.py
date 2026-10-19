"""Headless failures retain inspectable structural evidence before their channel notice."""

import json
import os

import pytest
from agent_comms.acp_failure import NativeTurnError
from agent_comms.diagnostics import record_terminal_failure


def test_terminal_diagnostic_excludes_untrusted_payloads(tmp_path):
    path = record_terminal_failure(
        tmp_path,
        turn_id="a" * 32,
        thread="worker",
        sequences=(6943,),
        event={
            "reason_code": "native_preflight_timeout",
            "text": "SECRET stderr data:image/png;base64,ABC123",
            "prompt": "private user text",
            "diagnostic": {
                "wait_ms": 5000,
                "session_bytes": 88255627,
                "exit_code": -15,
                "token": "SECRET",
                "spawn_ms": "SECRET",
            },
        },
    )
    raw = path.read_text()
    value = json.loads(raw)
    assert value["reason"] == "native_preflight_timeout"
    assert value["measurements"] == {"wait_ms": 5000, "session_bytes": 88255627, "exit_code": -15}
    assert value["sequences"] == [6943]
    assert all(fragment not in raw for fragment in ("SECRET", "private user", "base64"))
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.asyncio
async def test_headless_failure_publishes_original_error_without_report_file(native_backend):
    native = native_backend
    await native.author_history()
    native.provider.status = 400
    async with native.open_owner(runtime_enabled=True) as (owner, session):
        comms = owner._comms
        comms.registry.declare(comms.registry.new_thread(
            name="sender", tags=frozenset({"comms"}), worktree=str(native.project),
        ))
        incoming = comms.messaging.send("sender", session, "Private input").message
        with pytest.raises(NativeTurnError) as caught:
            await owner.turns.run_agent_turn(
                session, session, "Private input", origins=(incoming,),
            )
        detail = caught.value.request.failure.detail
        assert "maximum context length exceeded" in detail
        rows = [json.loads(line) for line in (comms.root / "bus.jsonl").read_text().splitlines()]
        notice = rows[-1]
        assert notice["notice"] is True
        assert detail in notice["text"]
        assert "Open diagnostic" not in notice["text"]
        assert not list((comms.root / "diagnostics").glob("*.json"))
        assert native.provider.posts == 1
