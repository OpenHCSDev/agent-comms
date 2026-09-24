"""ACP direct inputs remain visible and cannot cross a changed authority."""

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from agent_comms import Message, MessageType, Thread
from agent_comms.acp import CommsAgent
from agent_comms.declarations import ScheduledTurn
from agent_comms.input_disposition import InputDispositions
from agent_comms.operations import wire
from agent_comms.runtime import RuntimeProxy, socket_path


def test_each_direct_sequence_gets_its_own_native_turn():
    first = ScheduledTurn.incoming(
        Message(sender="peer", target="project", body="alpha", type=MessageType.INFO, seq=1)
    )
    second = ScheduledTurn.incoming(
        Message(sender="peer", target="project", body="beta", type=MessageType.INFO, seq=2)
    )
    batch, remaining = ScheduledTurn.take_batch([first, second])
    assert batch == [first]
    assert remaining == [second]


@pytest.mark.asyncio
async def test_direct_cannot_launch_text_backend_without_native_start_proof(tmp_path, monkeypatch):
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="/bin/echo", runtime_enabled=True)
    await agent.new_session(str(tmp_path / "project"))
    agent._drain_tasks["project"].cancel()
    await asyncio.gather(agent._drain_tasks["project"], return_exceptions=True)
    comms.register(Thread(name="peer", tags=frozenset(), worktree=str(tmp_path)))

    async def unexpected_backend(*args, **kwargs):
        raise AssertionError("Text backend launched for a direct without native proof")
        yield {}

    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", unexpected_backend)
    try:
        comms.send("peer", "project", "do not run unproved")
        assert await agent._drain_inbox("project") == 1
        assert not agent._pending_turns.get("project")
        assert not agent._wake_tasks.get("project")
        assert InputDispositions(comms.root).status("bus:1") == "unknown"
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_two_queued_directs_need_two_distinct_native_starts(tmp_path, monkeypatch):
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", runtime_enabled=True)
    await agent.new_session(str(tmp_path / "project"))
    agent._drain_tasks["project"].cancel()
    await asyncio.gather(agent._drain_tasks["project"], return_exceptions=True)
    monkeypatch.setattr(agent, "_schedule_wake", lambda _session: None)
    comms.register(Thread(name="peer", tags=frozenset(), worktree=str(tmp_path)))
    comms.send("peer", "project", "alpha")
    comms.send("peer", "project", "beta")
    assert await agent._drain_inbox("project") == 2
    assert len(agent._pending_turns["project"]) == 2
    receipts = []

    async def events(*args, **kwargs):
        native_id = f"{len(receipts) + 1:032x}"
        with kwargs["send_boundary"](None, native_id, args[2]) as allowed:
            assert allowed
        assert kwargs["native_start"](None, native_id, args[2])
        receipts.append(native_id)
        yield {"type": "input_started", "id": None}
        yield {"type": "done", "ok": True, "text": "done"}

    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
    try:
        CommsAgent._schedule_wake(agent, "project")
        await asyncio.wait_for(agent._wake_tasks["project"], timeout=3)
        assert receipts == [f"{1:032x}", f"{2:032x}"]
        assert InputDispositions(comms.root).status("bus:1") == "started"
        assert InputDispositions(comms.root).status("bus:2") == "started"
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_ui_ack_does_not_hide_unknown_or_authorize_goal_superseded_direct(
    tmp_path, monkeypatch
):
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", runtime_enabled=True)
    await agent.new_session(str(tmp_path / "project"))
    agent._drain_tasks["project"].cancel()
    await asyncio.gather(agent._drain_tasks["project"], return_exceptions=True)
    monkeypatch.setattr(agent, "_schedule_wake", lambda _session: None)
    comms.register(Thread(name="peer", tags=frozenset(), worktree=str(tmp_path)))
    comms.send("peer", "project", "review this")
    comms.acknowledge("project")  # Human/UI read is not model start.

    try:
        assert await agent._drain_inbox("project") == 1
        rows = InputDispositions(comms.root).unknown(frozenset({"project"}))
        assert [(row["sequence"], row["status"]) for row in rows] == [(1, "unknown")]
        assert len(agent._pending_turns["project"]) == 1
        comms.update_goal("project", "set", text="new goal")

        authorized = []

        async def events(*args, **kwargs):
            with kwargs["send_boundary"](None, "a" * 32, args[2]) as allowed:
                authorized.append(allowed)
            yield {"type": "done", "ok": False, "text": "not sent"}

        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
        pending = agent._pending_turns.pop("project")
        await agent._run_agent_turn(
            "project", "project", pending[0].prompt, origins=(pending[0].origin,)
        )
        assert authorized == [False]
        assert InputDispositions(comms.root).unknown(frozenset({"project"}))[0]["sequence"] == 1
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_project_change_after_queue_denies_stale_project_send(tmp_path, monkeypatch):
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", runtime_enabled=True)
    await agent.new_session(str(tmp_path / "project"))
    agent._drain_tasks["project"].cancel()
    await asyncio.gather(agent._drain_tasks["project"], return_exceptions=True)
    monkeypatch.setattr(agent, "_schedule_wake", lambda _session: None)
    comms.register(Thread(name="peer", tags=frozenset(), worktree=str(tmp_path)))
    comms.send("peer", "project", "use the intended project")
    await agent._drain_inbox("project")
    pending = agent._pending_turns.pop("project")[0]
    other_project = tmp_path / "other-project"
    other_project.mkdir()
    before = comms.registry.snapshot().admission_generations["project"]
    authorized = []

    async def events(*args, **kwargs):
        comms.set_project("project", str(other_project))
        with kwargs["send_boundary"](None, "a" * 32, args[2]) as allowed:
            authorized.append(allowed)
        yield {"type": "done", "ok": False, "text": "not sent"}

    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
    try:
        await agent._run_agent_turn("project", "project", pending.prompt, origins=(pending.origin,))
        assert authorized == [False]
        assert comms.registry.snapshot().admission_generations["project"] == before
        assert InputDispositions(comms.root).status("bus:1") == "unknown"
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_late_subscriber_receives_persisted_unknown(tmp_path):
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", runtime_enabled=True)
    await agent.new_session(str(tmp_path / "project"))
    agent._drain_tasks["project"].cancel()
    await asyncio.gather(agent._drain_tasks["project"], return_exceptions=True)
    agent._schedule_wake = lambda _session: None
    comms.register(Thread(name="peer", tags=frozenset(), worktree=str(tmp_path)))
    comms.send("peer", "project", "do the task")
    updates = []

    class LateClient:
        async def session_update(self, session_id, update):
            updates.append(
                update.model_dump(by_alias=True, exclude_none=True)
                if hasattr(update, "model_dump")
                else update
            )

    client = CommsAgent(comms, agent_bin="/bin/echo")
    client.on_connect(LateClient())
    proxy = None

    def assert_unknown_replayed():
        dispositions = [
            update.get("_meta", {}).get("agentComms", {}).get("inputDisposition", {})
            for update in updates
        ]
        assert any(
            row.get("sequence") == 1
            and row.get("target") == "project"
            and row.get("status") == "unknown"
            for row in dispositions
        )

    try:
        await agent._drain_inbox("project")
        # The projection runs on every platform; POSIX also checks the real
        # late socket subscribe route that invokes it.
        await agent.replay_unknown_inputs("project", client=LateClient())
        assert_unknown_replayed()
        if os.name != "nt":
            updates.clear()
            proxy = RuntimeProxy(client, "project", socket_path(comms.root, os.getpid()))
            await proxy.subscribe()
            assert_unknown_replayed()
    finally:
        if proxy is not None:
            await proxy.close()
        await agent.shutdown()


@pytest.mark.asyncio
async def test_stop_before_wake_leaves_direct_unknown_without_backend_send(tmp_path, monkeypatch):
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", runtime_enabled=True)
    await agent.new_session(str(tmp_path / "project"))
    agent._drain_tasks["project"].cancel()
    await asyncio.gather(agent._drain_tasks["project"], return_exceptions=True)
    monkeypatch.setattr(agent, "_schedule_wake", lambda _session: None)
    comms.register(Thread(name="peer", tags=frozenset(), worktree=str(tmp_path)))
    comms.send("peer", "project", "do not run after stop")
    await agent._drain_inbox("project")
    assert len(agent._pending_turns["project"]) == 1
    comms.stop("project")

    async def unexpected_backend(*args, **kwargs):
        raise AssertionError("Stopped owner launched a backend")
        yield {}

    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", unexpected_backend)
    try:
        CommsAgent._schedule_wake(agent, "project")
        await asyncio.wait_for(agent._wake_tasks["project"], timeout=2)
        assert not agent._pending_turns.get("project")
        assert InputDispositions(comms.root).unknown(frozenset({"project"}))[0]["sequence"] == 1
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX fake Pi executable")
async def test_started_then_ack_only_direct_survives_reopen_without_replay(tmp_path, monkeypatch):
    stub = tmp_path / "pi-direct-stub"
    session_file = tmp_path / "pi-session.jsonl"
    session_file.touch()
    stub.write_text(f"#!{sys.executable}\n" + f"session_file = {str(session_file)!r}\n" + """
import json, sys
send = lambda event: print(json.dumps(event), flush=True)
state = json.loads(sys.stdin.readline())
send({"type":"response", "command":"get_state", "id":state["id"],
      "success":True, "data":{"nativeInputProofCapability":"pi-native-input-v1-live-only",
      "sessionFile":session_file}})
prompt = json.loads(sys.stdin.readline())
send({"type":"response", "command":"prompt", "id":prompt["id"], "success":True})
send({"type":"message_start", "message":{"role":"user", "content":prompt["message"],
      "inputId":prompt["inputId"]}})
steer = json.loads(sys.stdin.readline())
send({"type":"response", "command":"prompt", "id":steer["id"], "success":True})
send({"type":"message_end", "message":{"role":"assistant", "stopReason":"stop"}})
send({"type":"agent_settled"})
sys.stdin.readline()
sys.stdin.readline()
send({"type":"response", "command":"get_session_stats", "success":True,
      "data":{"contextUsage":{}}})
""")
    stub.chmod(0o755)
    with tempfile.TemporaryDirectory(dir="/var/tmp") as wire_dir:
        comms = wire(Path(wire_dir))
        agent = CommsAgent(comms, agent_bin=str(stub), agent_args=[], runtime_enabled=True)
        await agent.new_session(str(tmp_path / "project"))
        agent._drain_tasks["project"].cancel()
        await asyncio.gather(agent._drain_tasks["project"], return_exceptions=True)
        monkeypatch.setattr(agent, "_schedule_wake", lambda _session: None)
        comms.register(Thread(name="peer", tags=frozenset(), worktree=str(tmp_path)))
        comms.send("peer", "project", "first direct")
        await agent._drain_inbox("project")
        first = agent._pending_turns.pop("project")[0]

        async def wait_for_inbox():
            async with asyncio.timeout(3):
                while "project" not in agent._backend_inboxes:
                    await asyncio.sleep(0.01)

        try:
            turn = asyncio.create_task(
                agent._run_agent_turn("project", "project", first.prompt, origins=(first.origin,))
            )
            await wait_for_inbox()
            comms.send("peer", "project", "second direct")
            await agent._drain_inbox("project")
            await asyncio.wait_for(turn, timeout=5)

            reopened = InputDispositions(comms.root)
            assert reopened.status("bus:1") == "started"
            assert reopened.status("bus:2") == "unknown"
            assert comms.registry.require("project").session_file == str(session_file)
            second_agent = CommsAgent(wire(comms.root), agent_bin="/bin/echo")
            second_agent._sessions["project"] = "project"
            second_agent._inbox_cursors["project"], second_agent._legacy_through["project"] = (
                second_agent._delivery_cursors.initialize(
                    frozenset({"project"}),
                    "project",
                    high_water=second_agent._comms.message_high_water(),
                    fresh=False,
                )
            )

            class SilentClient:
                async def session_update(self, **kwargs):
                    pass

            second_agent.on_connect(SilentClient())
            assert await second_agent._drain_inbox("project") == 0
            assert not second_agent._pending_turns.get("project")
        finally:
            await agent.shutdown()


@pytest.mark.skipif(os.name == "nt", reason="real crash test needs /var/tmp and POSIX sockets")
def test_hard_exit_after_direct_record_never_replays_on_reopen():
    child_code = """
import asyncio, os, sys
from pathlib import Path
from agent_comms import Thread
from agent_comms.acp import CommsAgent
from agent_comms.input_disposition import InputDispositions
from agent_comms.operations import wire

async def run():
    comms = wire(Path(sys.argv[1]))
    agent = CommsAgent(comms, agent_bin='/bin/echo', runtime_enabled=True)
    await agent.new_session(sys.argv[2])
    agent._drain_tasks['project'].cancel()
    await asyncio.gather(agent._drain_tasks['project'], return_exceptions=True)
    agent._schedule_wake = lambda _session: None
    comms.register(Thread(name='peer', tags=frozenset(), worktree=sys.argv[2]))
    comms.send('peer', 'project', 'survive hard exit')
    await agent._drain_inbox('project')
    assert InputDispositions(comms.root).status('bus:1') == 'unknown'
    os._exit(0)

asyncio.run(run())
"""
    with tempfile.TemporaryDirectory(dir="/var/tmp") as wire_dir:
        project = str(Path(wire_dir) / "project")
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
        child = subprocess.run(
            [sys.executable, "-c", child_code, wire_dir, project],
            env=environment,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert child.returncode == 0, child.stderr
        reopened = wire(Path(wire_dir))
        assert InputDispositions(reopened.root).status("bus:1") == "unknown"
        owner = CommsAgent(reopened, agent_bin="/bin/echo")
        owner._sessions["project"] = "project"
        owner._inbox_cursors["project"], owner._legacy_through["project"] = (
            owner._delivery_cursors.initialize(
                frozenset({"project"}),
                "project",
                high_water=reopened.message_high_water(),
                fresh=False,
            )
        )
        assert owner._inbox_cursors["project"] == 1

        class SilentClient:
            async def session_update(self, **kwargs):
                pass

        owner.on_connect(SilentClient())
        assert asyncio.run(owner._drain_inbox("project")) == 0
        assert not owner._pending_turns.get("project")
