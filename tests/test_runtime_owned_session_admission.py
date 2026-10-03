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
from agent_comms.runtime import RuntimeProxy, socket_path


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
        assert not (root / "input_dispositions.json").exists()

        # Use the original production publisher before another independent read.
        # The failed prompt is never resent or turned into a binding.
        await owner.sessions.bind_owned(thread, "original-acp-session")
        assert await proxy.request("goal_history") == {"history": []}
        comms.registry.rename("registered", "renamed-owner")
        assert await proxy.request("goal_history") == {"history": []}
        assert owner.sessions.require_owned_session(comms.registry.require("renamed-owner"), comms.registry.snapshot()) == "original-acp-session"
        assert owner.sessions.bindings == {"original-acp-session": "registered"}
        assert not (root / "native-sessions").exists()
        assert not (root / "input_dispositions.json").exists()
    finally:
        await proxy.close()
        await client.shutdown()
        await owner.shutdown()
    assert not socket_path(root, os.getpid()).exists()
