"""Project changes preserve thread/session identity and synchronize executing owners."""

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from acp import RequestError

from agent_comms import Thread, wire
from agent_comms.acp import CommsAgent
from agent_comms.tools import invoke_tool


def test_self_project_change_preserves_thread_and_rejects_invalid_paths(tmp_path, monkeypatch):
    old = tmp_path / "old"
    new = tmp_path / "new project"
    old.mkdir()
    new.mkdir()
    comms = wire(tmp_path / "wire")
    comms.register(
        Thread(
            name="worker",
            tags=frozenset({"test"}),
            worktree=str(old),
            pid=os.getpid(),
            session_file=str(old / "session.jsonl"),
            model="test/model",
            created_at=123,
        )
    )
    goal = comms.update_goal("worker", "set", text="Keep this objective")
    monkeypatch.setenv("PI_AGENT_ID", "worker")
    result = invoke_tool(comms, "comms_set_project", {"path": "../new project"})
    assert result["current"] == str(new) and result["changed"]
    thread = comms.registry.require("worker")
    assert thread.worktree == str(new)
    assert thread.pid == os.getpid() and thread.model == "test/model"
    assert thread.goal == goal and thread.created_at == 123
    assert thread.session_file == str(old / "session.jsonl")
    assert thread.previous_worktrees == (str(old),)
    assert not comms.set_project("worker", str(new)).changed
    for path in ("", str(tmp_path / "missing")):
        with pytest.raises(ValueError):
            comms.set_project("worker", path)
    file = tmp_path / "file"
    file.touch()
    with pytest.raises(ValueError, match="not a directory"):
        comms.set_project("worker", str(file))
    comms.rename_self("renamed")
    comms.attach_session("worker", str(old / "session.jsonl"))
    restored = wire(comms.root).registry.require("worker")
    assert restored.worktree == str(new) and restored.previous_worktrees == (str(old),)
    assert len(comms.registry.all_threads()) == 1


async def test_project_update_is_published_and_old_saved_cwd_can_resume(tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="/bin/echo", agent_args=[])
    session = (await agent.new_session(str(old))).session_id
    updates = []

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"])

    agent.on_connect(Client())
    comms.set_project(session, str(new))
    await agent._sync_session_identity(session)
    assert updates[-1].field_meta["agentComms"]["worktree"] == str(new)
    await agent.shutdown()
    resumed = CommsAgent(comms, agent_bin="/bin/echo", agent_args=[])
    try:
        result = await resumed.load_session(str(old), session)
        assert result.field_meta["agentComms"]["worktree"] == str(new)
        with pytest.raises(RequestError):
            await resumed.load_session(str(tmp_path / "unrelated"), session)
        assert len(comms.registry.all_threads()) == 1
    finally:
        await resumed.shutdown()


async def test_owner_automatically_continues_same_session_in_new_project(tmp_path, monkeypatch):
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="/bin/echo", agent_args=[], runtime_enabled=True)
    session = (await agent.new_session(str(old))).session_id
    session_file = str(old / "session.jsonl")
    calls = []

    async def events(*args, **kwargs):
        calls.append((args[3], kwargs.get("session_file"), args[2]))
        yield {"type": "agent_info", "session_file": session_file}
        if len(calls) == 1:
            comms.set_project(session, str(new))
            yield {"type": "tool_end", "id": "project", "name": "comms_set_project", "ok": True}
        yield {"type": "settled"}
        yield {"type": "done", "ok": True}

    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
    try:
        await agent.prompt(session, [{"type": "text", "text": "Change projects"}])
        agent._schedule_wake(session)
        await asyncio.wait_for(agent._wake_tasks[session], timeout=2)
        assert len(calls) == 2
        assert calls[0][0] == str(old)
        assert calls[1][0] == str(new) and calls[1][1] == session_file
        assert "Project change completed" in calls[1][2]
        assert comms.registry.require(session).pid == os.getpid()
        assert len(comms.registry.all_threads()) == 1
    finally:
        await agent.shutdown()


async def test_cancel_during_project_change_does_not_restart_work(tmp_path, monkeypatch):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="/bin/echo", agent_args=[])
    session = (await agent.new_session(str(old))).session_id
    changed = asyncio.Event()

    async def events(*args, **kwargs):
        comms.set_project(session, str(new))
        changed.set()
        await asyncio.sleep(60)
        yield {"type": "done", "ok": True}

    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
    prompt = asyncio.create_task(agent.prompt(session, [{"type": "text", "text": "switch"}]))
    try:
        await asyncio.wait_for(changed.wait(), timeout=2)
        await agent.cancel(session)
        assert (await prompt).stop_reason == "cancelled"
        assert comms.registry.require(session).worktree == str(new)
        assert not agent._pending_turns.get(session)
    finally:
        await agent.shutdown()


@pytest.mark.skipif(os.name == "nt", reason="UNIX runtime subscriptions")
async def test_project_changes_reach_all_subscribed_clients(tmp_path):
    from agent_comms.runtime import RuntimeProxy, socket_path

    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    comms = wire(tmp_path / "wire")
    owner = CommsAgent(comms, agent_bin="/bin/echo", agent_args=[], runtime_enabled=True)
    session = (await owner.new_session(str(old))).session_id
    proxies, updates = [], [[], []]

    class Client:
        def __init__(self, values):
            self.values = values

        async def session_update(self, session_id, update):
            self.values.append(update)

    try:
        for values in updates:
            client = CommsAgent(comms)
            client.on_connect(Client(values))
            proxy = RuntimeProxy(client, session, socket_path(comms.root, os.getpid()))
            proxies.append(proxy)
            assert (await proxy.subscribe())["agentComms"]["worktree"] == str(old)
        comms.set_project(session, str(new))
        await owner._sync_session_identity(session)
        async with asyncio.timeout(2):
            while not all(
                any(
                    u.get("_meta", {}).get("agentComms", {}).get("worktree") == str(new)
                    for u in values
                )
                for values in updates
            ):
                await asyncio.sleep(0.01)
        assert len(comms.registry.all_threads()) == 1
    finally:
        for proxy in proxies:
            await proxy.close()
        await owner.shutdown()


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is needed for the Pi bootstrap")
def test_pi_bootstrap_uses_sdk_cwd_override_without_affecting_other_node_programs(tmp_path):
    package = tmp_path / "pi-coding-agent"
    dist = package / "dist"
    core = dist / "core"
    core.mkdir(parents=True)
    (package / "package.json").write_text('{"type":"module"}')
    (core / "session-manager.js").write_text(
        "export class SessionManager { static open(path, dir, cwd) "
        '{ return cwd ?? "header-cwd"; } }'
    )
    cli = dist / "cli.js"
    cli.write_text(
        'import {SessionManager} from "./core/session-manager.js"; '
        'console.log(JSON.stringify([SessionManager.open("saved"), '
        'SessionManager.open("saved", null, "explicit")]));'
    )
    bootstrap = Path(__file__).parents[1] / "src/agent_comms/pi_project_bootstrap.mjs"
    env = {
        **os.environ,
        "AGENT_COMMS_MANAGED": "1",
        "PI_WORKTREE": str(tmp_path),
        "NODE_OPTIONS": f"--import={bootstrap.as_uri()}",
    }
    result = subprocess.check_output(["node", str(cli)], env=env, text=True)
    assert json.loads(result) == [str(tmp_path), "explicit"]
    (dist / "cli").mkdir()
    (dist / "cli" / "setup.js").write_text("export function setupCli() {}")
    (dist / "main.js").write_text(
        'import {SessionManager} from "./core/session-manager.js"; '
        'export async function main() { console.log(JSON.stringify([SessionManager.open("saved"), '
        'SessionManager.open("saved", null, "explicit")])); }'
    )
    (dist / "bundle").mkdir()
    bundled = dist / "bundle" / "cli.js"
    bundled.write_text('throw new Error("Bundled entry must not run twice");')
    result = subprocess.check_output(["node", str(bundled)], env=env, text=True)
    assert json.loads(result) == [str(tmp_path), "explicit"]
    assert (
        subprocess.check_output(
            ["node", "-"], input='console.log("stdin works")', env=env, text=True
        ).strip()
        == "stdin works"
    )
