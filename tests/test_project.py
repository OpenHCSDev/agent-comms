"""Project changes preserve thread/session identity and synchronize executing owners."""

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from acp import RequestError

from agent_comms import agent_events as ae
from delivery_owner_fixture import canonical_agent
from agent_comms.schedule_rules import WakeScheduleCheck
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.goal_actions import SetGoalAction
from agent_comms.threads import Thread
from agent_comms.acp_extension import CoordinationChangedUpdate, decode_updates
from agent_comms.tools import invoke_tool
from native_backend_fixture import native_backend_fixture


def test_self_project_change_preserves_thread_and_rejects_invalid_paths(tmp_path, monkeypatch):
    old = tmp_path / "old"
    new = tmp_path / "new project"
    old.mkdir()
    new.mkdir()
    comms = wire(tmp_path / "wire")
    comms.registry.declare(
        Thread(
            name="worker",
            tags=frozenset({"test"}),
            worktree=str(old),
            process_identity=ProcessIdentity.capture(os.getpid()),
            session_file=str(old / "session.jsonl"),
            model="test/model",
            created_at=123,
        )
    )
    goal = comms.goals.update_goal("worker", SetGoalAction(text="Keep this objective"))
    monkeypatch.setenv("PI_AGENT_ID", "worker")
    result = invoke_tool(comms, "comms_set_project", {"path": "../new project"})
    assert result["current"] == str(new) and result["changed"]
    thread = comms.registry.require("worker")
    assert thread.worktree == str(new)
    assert thread.pid == os.getpid() and thread.model == "test/model"
    assert thread.goal == goal and thread.created_at == 123
    assert thread.session_file == str(old / "session.jsonl")
    assert thread.previous_worktrees == (str(old),)
    assert not comms.threads.set_project("worker", str(new)).changed
    for path in ("", str(tmp_path / "missing")):
        with pytest.raises(ValueError):
            comms.threads.set_project("worker", path)
    file = tmp_path / "file"
    file.touch()
    with pytest.raises(ValueError, match="not a directory"):
        comms.threads.set_project("worker", str(file))
    comms.threads.rename_self("renamed")
    comms.threads.attach_session("worker", str(old / "session.jsonl"))
    restored = wire(comms.root).registry.require("worker")
    assert restored.worktree == str(new) and restored.previous_worktrees == (str(old),)
    assert len(comms.registry.all_threads()) == 1


async def test_project_update_is_published_and_old_saved_cwd_can_resume(tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    comms = wire(tmp_path / "wire")
    agent = canonical_agent(comms, agent_bin="/bin/echo", agent_args=[])
    session = (await agent.new_session(str(old))).session_id
    updates = []

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"])

    agent.on_connect(Client())
    comms.threads.set_project(session, str(new))
    await agent.sessions.sync_identity(session)
    assert next(
        update.worktree
        for update in decode_updates(updates[-1].field_meta)
        if isinstance(update, CoordinationChangedUpdate)
    ) == str(new)
    await agent.shutdown()
    resumed = canonical_agent(comms, agent_bin="/bin/echo", agent_args=[])
    try:
        result = await resumed.load_session(str(old), session)
        assert next(
            update.worktree
            for update in decode_updates(result.field_meta)
            if isinstance(update, CoordinationChangedUpdate)
        ) == str(new)
        with pytest.raises(RequestError):
            await resumed.load_session(str(tmp_path / "unrelated"), session)
        assert len(comms.registry.all_threads()) == 1
    finally:
        await resumed.shutdown()


async def test_saved_native_identity_changes_publish_one_complete_update(tmp_path):
    """Original saved owner, actual socket publication; no native input/provider."""
    from agent_comms.runtime import RuntimeProxy, socket_path

    class Client:
        def __init__(self):
            self.updates = []
            self.fail = False

        async def session_update(self, *, session_id, update):
            if self.fail:
                raise OSError("Identity publication transport failed")
            self.updates.append(update)

    async with native_backend_fixture(tmp_path) as native:
        await native.author_history()
        saved = native.session.read_bytes()
        async with native.open_owner() as (agent, session_id):
            await agent._runtime.start()
            sender, receiver = Client(), Client()
            agent.on_connect(sender)
            attached = canonical_agent(agent._comms, auto_wake=False)
            attached.on_connect(receiver)
            proxy = RuntimeProxy(attached, session_id, socket_path(native.root, os.getpid()))
            try:
                await proxy.subscribe()
                sender.updates.clear()
                receiver.updates.clear()
                new = tmp_path / "new-project"
                new.mkdir()
                agent._comms.threads.set_project(session_id, str(new))
                renamed = agent._comms.threads.rename_managed_thread(
                    session_id, "Updated owner", owner_pid=os.getpid(),
                ).current
                before = (agent.sessions.titles.get(session_id),
                          agent.sessions.display_titles.get(session_id),
                          agent.sessions.worktrees.get(session_id))
                sender.fail = True
                with pytest.raises(OSError, match="transport failed"):
                    await agent.sessions.sync_identity(session_id)
                assert (agent.sessions.titles.get(session_id),
                        agent.sessions.display_titles.get(session_id),
                        agent.sessions.worktrees.get(session_id)) == before
                sender.fail = False
                assert await agent.sessions.sync_identity(session_id) == renamed
                assert len(sender.updates) == 1
                announcement = sender.updates[0]
                assert announcement.title == "Updated owner"
                projection, = (item for item in decode_updates(announcement.field_meta)
                               if isinstance(item, CoordinationChangedUpdate))
                assert projection.thread.name == renamed and projection.worktree == str(new)
                async with asyncio.timeout(2):
                    while not receiver.updates:
                        await asyncio.sleep(0.01)
                received, = (item for item in decode_updates(receiver.updates[-1].get("_meta"))
                             if isinstance(item, CoordinationChangedUpdate))
                assert received == projection
                assert agent.sessions.titles[session_id] == renamed
                assert agent.sessions.display_titles[session_id] == "Updated owner"
                assert agent.sessions.worktrees[session_id] == str(new)
                assert await agent.sessions.sync_identity(session_id) == renamed
                assert len(sender.updates) == 1
                assert agent._comms.registry.require(session_id).active_turn is None
            finally:
                await proxy.close()
                await attached.shutdown()
            assert native.session.read_bytes() == saved
            assert native.provider.posts == 0 and native.starts == []
    assert all(not child.alive() and not child.platform.group_members(child.identity)
               for child in native.children)


async def test_owner_automatically_continues_same_session_in_new_project(tmp_path, monkeypatch):
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    comms = wire(tmp_path / "wire")
    agent = canonical_agent(comms, agent_bin="/bin/echo", agent_args=[], runtime_enabled=True)
    session = (await agent.new_session(str(old))).session_id
    session_file = str(old / "session.jsonl")
    calls = []

    async def events(*args, **kwargs):
        calls.append((args[3], kwargs.get("session_file"), args[2]))
        yield ae.AgentInfo(session_file=session_file)
        if len(calls) == 1:
            comms.threads.set_project(session, str(new))
            yield ae.ToolEnd(id="project", name="comms_set_project", ok=True)
        yield ae.StreamSettled()
        yield ae.Done(ok=True, text="")

    monkeypatch.setattr("agent_comms.backend.stream_agent_events", events)
    try:
        await agent.prompt(session, [{"type": "text", "text": "Change projects"}])
        WakeScheduleCheck(session_id=session, inputs=agent.inputs).schedule()
        await asyncio.wait_for(agent.inputs.wake_tasks[session], timeout=2)
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
    agent = canonical_agent(comms, agent_bin="/bin/echo", agent_args=[])
    session = (await agent.new_session(str(old))).session_id
    changed = asyncio.Event()

    async def events(*args, **kwargs):
        comms.threads.set_project(session, str(new))
        changed.set()
        await asyncio.sleep(60)
        yield ae.Done(ok=True, text="")

    monkeypatch.setattr("agent_comms.backend.stream_agent_events", events)
    prompt = asyncio.create_task(agent.prompt(session, [{"type": "text", "text": "switch"}]))
    try:
        await asyncio.wait_for(changed.wait(), timeout=2)
        await agent.cancel(session)
        assert (await prompt).stop_reason == "cancelled"
        assert comms.registry.require(session).worktree == str(new)
        assert not agent.inputs.pending_turns.get(session)
    finally:
        await agent.shutdown()


@pytest.mark.skipif(os.name == "nt", reason="UNIX runtime subscriptions")
async def test_project_changes_reach_all_subscribed_clients(tmp_path):
    from agent_comms.runtime import RuntimeProxy, socket_path

    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    comms = wire(tmp_path / "wire")
    owner = canonical_agent(comms, agent_bin="/bin/echo", agent_args=[], runtime_enabled=True)
    session = (await owner.new_session(str(old))).session_id
    proxies, updates = [], [[], []]

    class Client:
        def __init__(self, values):
            self.values = values

        async def session_update(self, session_id, update):
            self.values.append(update)

    try:
        for values in updates:
            client = canonical_agent(comms)
            client.on_connect(Client(values))
            proxy = RuntimeProxy(client, session, socket_path(comms.root, os.getpid()))
            proxies.append(proxy)
            assert next(
                update.worktree
                for update in decode_updates(await proxy.subscribe())
                if isinstance(update, CoordinationChangedUpdate)
            ) == str(old)
        comms.threads.set_project(session, str(new))
        await owner.sessions.sync_identity(session)
        async with asyncio.timeout(2):
            while not all(
                any(
                    update.worktree == str(new)
                    for value in values
                    for update in decode_updates(value.get("_meta"))
                    if isinstance(update, CoordinationChangedUpdate)
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
