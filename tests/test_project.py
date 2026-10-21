"""Project changes preserve thread/session identity and synchronize executing owners."""

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from acp import RequestError

from delivery_owner_fixture import canonical_agent
from agent_comms.comms import wire
from agent_comms.acp_extension import CoordinationChangedUpdate, decode_updates
from agent_comms.native_arguments import GivenArguments, GivenBinary




async def test_project_update_is_published_and_old_saved_cwd_can_resume(tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    comms = wire(tmp_path / "wire")
    agent = canonical_agent(comms, agent_bin=GivenBinary("/bin/echo"), agent_args=GivenArguments(()))
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
    resumed = canonical_agent(comms, agent_bin=GivenBinary("/bin/echo"), agent_args=GivenArguments(()))
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




@pytest.mark.skipif(os.name == "nt", reason="UNIX runtime subscriptions")
async def test_project_changes_reach_all_subscribed_clients(tmp_path):
    from agent_comms.runtime import RuntimeProxy, socket_path

    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    comms = wire(tmp_path / "wire")
    owner = canonical_agent(comms, agent_bin=GivenBinary("/bin/echo"), agent_args=GivenArguments(()), runtime_enabled=True)
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
