"""PID registration is not an original loaded-session resource."""

import asyncio
import json
import os
import subprocess
import sys

import pytest
from acp import RequestError

from agent_comms.acp import CommsAgent
from agent_comms.comms import Comms
from agent_comms.child_process import ProcessIdentity
from agent_comms.errors import UnregisteredThreadError
from agent_comms.runtime import RuntimeProxy, socket_path
from agent_comms.runtime_requests import GoalHistoryRuntimeRequest
from agent_comms.threads import Thread


@pytest.mark.skipif(os.name == "nt", reason="original Unix owner socket")
async def test_cli_registration_refuses_unbound_then_uses_original_owned_session(tmp_path):
    root = tmp_path / "wire"
    project = tmp_path / "project"
    project.mkdir()
    comms = Comms(root)
    owner = CommsAgent(comms, runtime_enabled=True, auto_wake=False)
    client = CommsAgent(comms, auto_wake=False)
    registered = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "agent_comms.cli", "--root", str(root),
         "register", "--name", "registered", "--worktree", str(project),
         "--pid", str(os.getpid())],
        capture_output=True, text=True, check=True,
    )
    assert json.loads(registered.stdout) == {"registered": "registered"}
    thread = comms.registry.require("registered")
    await owner._runtime.start()
    proxy = RuntimeProxy(client, "registered", socket_path(root, os.getpid()))
    try:
        for action, parameters in (
            ("prompt", {"prompt": [{"type": "text", "text": "Unbound input must never execute"}]}),
            ("goal_history", {}),
        ):
            with pytest.raises(RequestError) as caught:
                await proxy.request(action, **parameters)
            assert caught.value.code == -32602
            assert caught.value.data["owner"] == "registered"
            assert "no loaded ACP session" in caught.value.data["reason"]
        assert not owner.sessions.bindings
        assert not owner._runtime.clients
        assert comms.registry.require("registered") == thread
        assert not (root / "native-sessions").exists()
        assert not (root / "input_dispositions.sqlite3").exists()

        retired = Thread("retired-owner", frozenset(), str(project),
                         process_identity=ProcessIdentity.capture(os.getpid()))
        comms.registry.declare(retired)
        await owner.sessions.bind_owned(retired, "retired-acp-session")
        comms.registry.remove(retired.name)
        snapshot = comms.registry.snapshot()
        assert snapshot.canonical_name(retired.name) == retired.name
        assert comms.registry.canonical_name(retired.name) == retired.name
        with pytest.raises(UnregisteredThreadError):
            GoalHistoryRuntimeRequest(thread=retired.name).require_owner(snapshot)

        # Use the original production publisher before another independent read.
        # The failed prompt is never resent or turned into a binding.
        await owner.sessions.bind_owned(thread, "original-acp-session")
        assert await proxy.request("goal_history") == {"history": []}
        comms.registry.rename("registered", "renamed-owner")
        assert await proxy.request("goal_history") == {"history": []}
        assert owner.sessions.require_owned_session(comms.registry.require("renamed-owner"), comms.registry.snapshot()) == "original-acp-session"
        assert owner.sessions.bindings == {
            "retired-acp-session": "retired-owner",
            "original-acp-session": "registered",
        }
        assert not (root / "native-sessions").exists()
        assert not (root / "input_dispositions.sqlite3").exists()
    finally:
        await proxy.close()
        await client.shutdown()
        await owner.shutdown()
    assert not socket_path(root, os.getpid()).exists()


@pytest.mark.parametrize("replacement", ("rename", "remove", "recreate", "new_lease"))
async def test_shutdown_releases_only_the_acquired_owner(tmp_path, replacement):
    comms = Comms(tmp_path / "wire")
    owner = CommsAgent(comms, runtime_enabled=False, auto_wake=False)
    thread = owner.sessions.declare_thread(str(tmp_path / "original"))
    await owner.sessions.bind_owned(thread, "original-session")
    if replacement == "rename":
        comms.registry.rename(thread.name, "renamed")
    elif replacement in {"remove", "recreate"}:
        comms.registry.remove(thread.name)
        if replacement == "recreate":
            comms.registry.declare(Thread(
                thread.name, frozenset(), thread.worktree,
                process_identity=ProcessIdentity.capture(os.getpid()),
            ))
    else:
        comms.registry.register(thread, new_owner=True)
    await owner.shutdown()
    assert not owner.sessions.owned
    if replacement == "rename":
        assert not comms.registry.status("renamed").running
    elif replacement == "remove":
        assert thread.name not in comms.registry.snapshot().threads
    else:
        current = comms.registry.require(thread.name)
        assert current.require_process() == thread.require_process()
        assert comms.registry.status(current.name).running
