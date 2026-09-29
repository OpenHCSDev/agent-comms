"""Actual fork worker held before its socket, with the pinned native model on loopback."""

import asyncio
import json
import os
import shlex
import signal
import sys
from pathlib import Path

import pytest

from agent_comms.acp import CommsClient
from agent_comms.child_process import DetachedProcess, Platform
from agent_comms.comms import Comms
from agent_comms.runtime import socket_path
from agent_comms.thread_management import ForkSpec
from agent_comms.threads import Thread

pytest_plugins = ("test_backend_native_lifecycle",)


async def test_actual_fork_attachment_survives_cold_owner_before_socket(
    native_backend, monkeypatch
):
    fixture = native_backend
    await fixture.run("Parent saved history for cold attachment")
    await fixture.persistent.close()
    fixture.provider.text = "Cold fork first reply received."
    comms = Comms(fixture.root)
    package = Path(os.environ["PI_COMPACTION_TEST_PACKAGE"])
    launcher = str(Path(sys.executable).with_name("pi-comms-native"))
    arguments = [
        "--provider",
        "response-local",
        "--model",
        "fixture",
        "--thinking",
        "off",
        "--offline",
        "--no-extensions",
        "--no-skills",
        "--no-context-files",
        "--no-prompt-templates",
        "--no-tools",
    ]
    monkeypatch.setenv("AGENT_COMMS_AGENT_ARGS", shlex.join(arguments))
    with comms.bus.log.locked():
        root_id = comms.bus.log.read_metadata_unlocked().root_id
    comms.owners.pin_private_nk_launch(fixture.root, root_id, package)
    comms.threads.register(
        Thread(
            "parent",
            frozenset(),
            str(fixture.project),
            session_file=str(fixture.session),
            model="response-local/fixture",
            thinking_level="off",
        )
    )
    launched = []
    launch = DetachedProcess.launch

    def hold_worker(*args, **kwargs):
        child = launch(*args, **kwargs)
        if kwargs["env"].get("AGENT_COMMS_THREAD") == "cold-fork":
            Platform.current().send(child.identity, signal.SIGSTOP)
            launched.append(child)
        return child

    monkeypatch.setattr(DetachedProcess, "launch", hold_worker)
    client = CommsClient(
        comms,
        agent_bin=launcher,
        agent_args=arguments,
        auto_wake=False,
        private_nk_native_package=package,
        private_nk_wire_root_id=root_id,
    )
    updates = []

    class View:
        async def session_update(self, **kwargs):
            updates.append(kwargs)

    client.on_connect(View())
    loading = None
    try:
        child = await asyncio.to_thread(
            comms.threads.fork,
            ForkSpec("cold-fork", "parent", "One new cold fork diagnostic input"),
            launcher,
        )
        endpoint = socket_path(fixture.root, child.pid)
        assert len(launched) == 1 and not endpoint.exists()
        loading = asyncio.create_task(client.load_session(str(fixture.project), child.name))
        await asyncio.sleep(5.4)
        assert not loading.done(), "live registered startup expired before its socket existed"
        assert not endpoint.exists()
        assert comms.registry.require(child.name).process_identity == child.process_identity
        Platform.current().send(child.require_process(), signal.SIGCONT)
        async with asyncio.timeout(35):
            await loading
            while not comms.registry.require(child.name).last_finished_turn_id:
                await asyncio.sleep(0.05)
            while "Cold fork first reply received." not in json.dumps(updates, default=str):
                await asyncio.sleep(0.05)
        saved = [json.loads(line) for line in Path(child.session_file).read_text().splitlines()]
        inputs = [
            row["message"]["content"]
            for row in saved
            if row.get("type") == "message" and row["message"].get("role") == "user"
        ]
        assert len(inputs) == 2
        assert "One new cold fork diagnostic input" in str(inputs[-1])
        assert comms.registry.require(child.name).process_identity == child.process_identity
        assert fixture.provider.posts == 2  # Parent seed, then exactly one new fork input.
        print(
            "ACTUAL_COLD_FORK: held 5.4s pre-socket; same worker; saved parent + exactly one input; reply complete"
        )
    finally:
        for process in launched:
            if process.alive():
                Platform.current().send(process.identity, signal.SIGCONT)
        if loading is not None:
            if not loading.done():
                loading.cancel()
            await asyncio.gather(loading, return_exceptions=True)
        await client.shutdown()
        for process in launched:
            await process.stop()
