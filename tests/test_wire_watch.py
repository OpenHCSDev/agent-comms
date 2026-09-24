"""An idle owner wakes for durable wire changes without a 20 Hz scan."""

import asyncio

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.operations import wire
from agent_comms.wire_watch import open_wire_watcher


@pytest.mark.asyncio
async def test_idle_owner_wakes_on_bus_append_without_polling(tmp_path, monkeypatch):
    probe = open_wire_watcher(tmp_path)
    if probe is None:
        pytest.skip("Native file notifications are unavailable")
    probe.close()

    agent = CommsAgent(wire(tmp_path), runtime_enabled=False)
    calls = 0
    woke = asyncio.Event()

    async def drain(_session_id):
        nonlocal calls
        calls += 1
        if calls >= 2:
            woke.set()
        return 0

    async def noop(_session_id=None):
        return None

    monkeypatch.setattr(agent, "_drain_inbox", drain)
    monkeypatch.setattr(agent, "_sync_thread_config", noop)
    monkeypatch.setattr(agent, "_schedule_goal", lambda _session_id: None)
    monkeypatch.setattr(agent, "_refresh_auth_models", noop)
    agent._ensure_live_drain("owner")
    task = agent._drain_tasks["owner"]
    try:
        await asyncio.sleep(0.2)
        assert calls == 1
        with (tmp_path / "thread_read_markers.json").open("wb") as output:
            output.write(b"{}")
        await asyncio.sleep(0.1)
        assert calls == 1  # A viewer cursor is not new work for the owner.
        with (tmp_path / "bus.jsonl").open("ab") as output:
            output.write(b"{}\n")
        await asyncio.wait_for(woke.wait(), timeout=0.5)
        assert calls == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
