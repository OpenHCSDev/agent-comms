"""Headless failures retain inspectable structural evidence before their channel notice."""

import json
import os

import pytest

from agent_comms import Thread, wire
from agent_comms.acp import CommsAgent
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
async def test_headless_failure_publishes_reference_after_durable_diagnostic(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = wire(tmp_path / "wire")
    owner = CommsAgent(comms, agent_bin="pi", auto_wake=False)
    monkeypatch.setattr(owner, "_ensure_live_drain", lambda _: None)
    session = (await owner.new_session(str(tmp_path))).session_id
    comms.register(Thread(name="sender", tags=frozenset(), worktree=str(tmp_path), pid=os.getpid()))
    incoming = comms.send_message("sender", "#comms", "Private input")
    original_send = comms.send
    observed = []

    def publish(*args, **kwargs):
        files = list((comms.root / "diagnostics").glob("*.json"))
        assert len(files) == 1
        observed.append(json.loads(files[0].read_text()))
        return original_send(*args, **kwargs)

    async def events(*args, **kwargs):
        yield {
            "type": "done",
            "ok": False,
            "text": "SECRET native stderr",
            "reason_code": "native_preflight_timeout",
            "diagnostic": {"wait_ms": 5000},
        }

    monkeypatch.setattr(comms, "send", publish)
    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
    await owner._run_agent_turn(
        session, session, "Private input", origins=(incoming,), reply_targets=("#comms",)
    )
    assert observed[0]["sequences"] == [incoming.seq]
    assert observed[0]["reason"] == "native_preflight_timeout"
    rows = [json.loads(line) for line in (comms.root / "bus.jsonl").read_text().splitlines()]
    notice = rows[-1]
    assert notice["notice"] is True
    assert "[Open diagnostic](file://" in notice["text"]
    assert "SECRET" not in notice["text"]
    assert (
        owner._dispositions.status(
            owner._dispositions.bus_key(incoming, comms.registry.require(session))
        )
        == "unknown"
    )
    await owner.shutdown()
