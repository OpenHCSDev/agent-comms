"""Actual native owner survives client loss; a loopback provider gates completion."""

import asyncio
import json
import os
from contextlib import suppress
from pathlib import Path

import pytest

from agent_comms.acp import CommsClient
from agent_comms.child_process import DetachedProcess
from agent_comms.comms import wire
from compaction_loopback import LoopbackProvider


@pytest.mark.skipif(os.name == "nt", reason="POSIX detached owner")
async def test_new_thread_survives_client_loss_and_reattaches_without_duplicate(tmp_path, monkeypatch):
    package = Path(os.environ["PI_COMPACTION_TEST_PACKAGE"])
    project = tmp_path / "project"
    project.mkdir()
    started, release, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    provider = LoopbackProvider(status=200)

    async def handle(reader, writer):
        started.set()
        await release.wait()
        await provider.handle(reader, writer)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    profile = tmp_path / "profile"
    profile.mkdir(mode=0o700)
    model = {"providers": {"openrouter": {
        "baseUrl": f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1",
        "apiKey": "local-fixture", "api": "openai-completions",
        "models": [{"id": "fake-compact", "contextWindow": 128000,
                    "maxTokens": 4096, "reasoning": False,
                    "compat": {"supportsUsageInStreaming": False}}],
    }}}
    for name, content in {
        "models.json": model,
        "auth.json": {},
        "settings.json": {"retry": {"enabled": False, "maxRetries": 0}},
    }.items():
        path = profile / name
        path.write_text(json.dumps(content))
        path.chmod(0o600)
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parents[1] / "src"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(profile))
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "openrouter/fake-compact")
    monkeypatch.setenv("PI_OFFLINE", "1")
    owner_output = (tmp_path / "owner-output.log").open("wb")
    launch = DetachedProcess.launch

    def logged_launch(*args, **kwargs):
        return launch(*args, **{**kwargs, "output": owner_output})

    monkeypatch.setattr(DetachedProcess, "launch", logged_launch)
    comms = wire(tmp_path / "wire")
    root_id = comms.messaging.initialize_private_initial_protocol()
    comms.owners.pin_private_nk_launch(comms.root, root_id, package)
    options = dict(agent_bin=str(package / "dist/cli.js"),
                   agent_args=["--provider", "openrouter", "--model", "fake-compact"],
                   private_nk_native_package=package, private_nk_wire_root_id=root_id)
    first, second, third = (CommsClient(comms, **options) for _ in range(3))

    class Client:
        async def session_update(self, session_id, update):
            if update.get("_meta", {}).get("agentComms", {}).get("turnSettled"):
                settled.set()

    first.on_connect(Client())
    second.on_connect(Client())
    name, owner, turn = None, None, None
    try:
        name = (await first.new_session(str(project))).session_id
        owner = comms.registry.require(name).pid
        assert owner != os.getpid() and os.getpgid(owner) == owner
        turn = asyncio.create_task(first.prompt(name, [{"type": "text", "text": "work"}]))
        await asyncio.wait_for(started.wait(), 15)
        await first.shutdown()
        turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
        assert comms.registry.require(name).process_alive and comms.registry.require(name).executing
        attachments = await asyncio.gather(
            second.load_session(str(project), name), third.load_session(str(project), name)
        )
        assert all(item.field_meta["agentComms"]["ownerPid"] == owner for item in attachments)
        settled.clear()
        release.set()
        await asyncio.wait_for(settled.wait(), 15)
        assert provider.posts == 1
        assert not comms.registry.require(name).executing
        await second.shutdown()
        await third.shutdown()
        assert comms.registry.require(name).process_alive
        assert comms.registry.require(name).pid == owner
    finally:
        release.set()
        if turn is not None:
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)
        for client in (first, second, third):
            await client.shutdown()
        if name is None and "project" in comms.registry:
            name = "project"
            owner = comms.registry.require(name).pid
        if name is not None:
            comms.owners.stop(name)
        if owner is not None:
            with suppress(ChildProcessError):
                await asyncio.to_thread(os.waitpid, owner, 0)
        owner_output.close()
        server.close()
        await server.wait_closed()
