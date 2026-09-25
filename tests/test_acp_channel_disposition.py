"""Channel model requests have per-recipient durable native-input outcomes."""

import asyncio
import os

import pytest

from agent_comms import wire
from agent_comms.acp import CommsAgent
from agent_comms.input_disposition import InputDispositions


@pytest.mark.parametrize("revocation", ["goal", "stop", "reopen"])
async def test_channel_queued_before_revocation_remains_visible_unknown(
    tmp_path, monkeypatch, revocation
):
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", runtime_enabled=True)
    monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)
    monkeypatch.setattr(agent, "_schedule_wake", lambda _session: None)
    await agent.new_session(str(tmp_path / "worker"))
    comms.update_tags("worker", add=frozenset({"team"}))
    message = comms.send_user_message("#team", "Durable request", worktree=str(tmp_path))
    observed = []
    advance = agent._delivery_cursors.advance

    def durable_before_cursor(aliases, through):
        if through >= message.seq:
            rows = InputDispositions(comms.root).unknown(frozenset({"worker"}))
            assert [row["sequence"] for row in rows] == [message.seq]
            observed.append(through)
        advance(aliases, through)

    monkeypatch.setattr(agent._delivery_cursors, "advance", durable_before_cursor)
    try:
        await agent._drain_inbox("worker")
        assert observed
        assert len(agent._pending_turns["worker"]) == 1
        if revocation == "goal":
            comms.update_goal("worker", "set", text="New goal")
        elif revocation == "stop":
            comms.stop("worker")
        else:
            await agent.shutdown()
            agent = CommsAgent(wire(comms.root), agent_bin="pi", runtime_enabled=True)
            monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)
            await agent.load_session(str(tmp_path / "worker"), "worker")
            await agent._drain_inbox("worker")
        if revocation != "reopen":
            CommsAgent._schedule_wake(agent, "worker")
            await asyncio.wait_for(agent._wake_tasks["worker"], 2)
        assert not agent._pending_turns.get("worker")
        updates = []

        class Client:
            async def session_update(self, **kwargs):
                updates.append(kwargs["update"].model_dump(by_alias=True))

        await agent.replay_unknown_inputs("worker", Client())
        unknown = [row["_meta"]["agentComms"]["inputDisposition"] for row in updates]
        assert [(row["sequence"], row["target"], row["status"]) for row in unknown] == [
            (message.seq, "#team", "unknown")
        ]
    finally:
        await agent.shutdown()


async def test_channel_native_receipts_are_per_recipient_and_per_sequence(tmp_path, monkeypatch):
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", runtime_enabled=True)
    monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)
    for name in ("alpha", "beta"):
        await agent.new_session(str(tmp_path / name))
        comms.update_tags(name, add=frozenset({"team"}))
    calls = []

    async def events(*args, **kwargs):
        text = args[2]
        calls.append((args[4]["AGENT_COMMS_THREAD"], text))
        native = f"{len(calls):032x}"
        with kwargs["send_boundary"](None, native, text) as allowed:
            assert allowed is True
        assert kwargs["native_start"](None, native, text)
        yield {"type": "input_started", "id": None}
        yield {"type": "settled"}
        yield {"type": "done", "ok": True, "text": "Received"}

    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
    try:
        messages = [
            comms.send_user_message("#team", text, worktree=str(tmp_path))
            for text in ("Request one", "Request two")
        ]
        for name in ("alpha", "beta"):
            await agent._drain_inbox(name)
        await asyncio.gather(*(agent._wake_tasks[name] for name in ("alpha", "beta")))
        rows = list(InputDispositions(comms.root)._read().values())
        assert len(rows) == 4
        assert {(row["owner"], row["sequence"], row["status"]) for row in rows} == {
            (name, message.seq, "started") for name in ("alpha", "beta") for message in messages
        }
        assert len({row["native_id"] for row in rows}) == len(calls) == 2
        assert all("Request one" in text and "Request two" in text for _, text in calls)
    finally:
        await agent.shutdown()


@pytest.mark.skipif(os.name == "nt", reason="POSIX crash durability uses /var/tmp")
@pytest.mark.parametrize("crash_at", ["before_cursor", "after_queue"])
async def test_channel_hard_exit_never_replays_unknown(crash_at, monkeypatch):
    import subprocess
    import sys
    from pathlib import Path
    from tempfile import TemporaryDirectory

    code = """
import asyncio, os, sys
from pathlib import Path
from agent_comms import wire
from agent_comms.acp import CommsAgent

async def run():
    comms = wire(Path(sys.argv[1]))
    agent = CommsAgent(comms, agent_bin="pi", runtime_enabled=True)
    agent._ensure_live_drain = lambda session: None
    agent._schedule_wake = lambda session: None
    await agent.new_session(sys.argv[2])
    comms.update_tags("worker", add=frozenset({"team"}))
    message = comms.send_user_message("#team", "CRASH_REQUEST", worktree=sys.argv[2])
    advance = agent._delivery_cursors.advance
    def before_cursor(aliases, through):
        if through == message.seq and sys.argv[3] == "before_cursor":
            assert agent._dispositions.unknown(frozenset({"worker"}))[0]["sequence"] == message.seq
            os._exit(0)
        advance(aliases, through)
    agent._delivery_cursors.advance = before_cursor
    await agent._drain_inbox("worker")
    assert agent._pending_turns["worker"]
    assert agent._dispositions.unknown(frozenset({"worker"}))[0]["sequence"] == message.seq
    os._exit(0)
asyncio.run(run())
"""
    with TemporaryDirectory(prefix="ac-channel-crash-", dir="/var/tmp") as raw:
        root = Path(raw)
        environment = os.environ.copy()
        for name in (
            "PI_AGENT_ID",
            "PI_PARENT_ID",
            "PI_TASK",
            "PI_WORKTREE",
            "AGENT_COMMS_THREAD",
            "AGENT_COMMS_MANAGED",
            "AGENT_COMMS_ROOT",
        ):
            environment.pop(name, None)
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        result = subprocess.run(
            [sys.executable, "-c", code, str(root / "wire"), str(root / "worker"), crash_at],
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        comms = wire(root / "wire")
        agent = CommsAgent(comms, agent_bin="pi", runtime_enabled=True)
        monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)
        try:
            await agent.load_session(str(root / "worker"), "worker")
            await agent._drain_inbox("worker")
            assert not agent._pending_turns.get("worker")
            assert not agent._wake_tasks.get("worker")
            rows = agent._dispositions.unknown(frozenset({"worker"}))
            assert len(rows) == 1 and rows[0]["native_id"] is None
            assert rows[0]["source_text"].endswith("CRASH_REQUEST")
        finally:
            await agent.shutdown()


@pytest.mark.parametrize("mismatch", ["original", "native", "duplicate"])
async def test_channel_batch_never_credits_omitted_or_duplicate_sequences(
    tmp_path, monkeypatch, mismatch
):
    from agent_comms.declarations import ScheduledTurn

    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", runtime_enabled=True)
    monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)
    monkeypatch.setattr(agent, "_schedule_wake", lambda _session: None)
    await agent.new_session(str(tmp_path / "worker"))
    comms.update_tags("worker", add=frozenset({"team"}))
    messages = tuple(
        comms.send_user_message("#team", body, worktree=str(tmp_path))
        for body in ("FIRST", "SECOND")
    )
    await agent._drain_inbox("worker")
    prompt = "\n\n".join(ScheduledTurn.incoming(message).prompt for message in messages)
    if mismatch == "original":
        prompt = ScheduledTurn.incoming(messages[0]).prompt
    origins = messages if mismatch != "duplicate" else (messages[0], messages[0])

    async def events(*args, **kwargs):
        text = args[2] if mismatch != "native" else args[2].replace("SECOND", "OMITTED")
        with kwargs["send_boundary"](None, "a" * 32, text) as allowed:
            assert allowed is False
        assert not kwargs["native_start"](None, "a" * 32, text)
        yield {"type": "done", "ok": False, "text": "Refused malformed batch"}

    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
    try:
        await agent._run_agent_turn("worker", "worker", prompt, origins=origins)
        rows = agent._dispositions.unknown(frozenset({"worker"}))
        assert {row["sequence"] for row in rows} == {message.seq for message in messages}
        assert all(row["native_id"] is None for row in rows)
    finally:
        await agent.shutdown()
