"""Retained native execution, refusal, and canonical headless/runtime behavior."""


import asyncio
import os
import sys
from pathlib import Path

import pytest
from acp.schema import TextContentBlock

import agent_comms.agent_events as events
from agent_comms.acp_extension import CoordinationChangedUpdate, decode_updates
from agent_comms.execution.launch_root import LaunchRoot
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread
from bus_history import read_history
from bus_record_fixture import prepare_native_comms
from pi_session_turn import one_turn_events


@pytest.mark.parametrize("arguments,code", [(('--help',), 0), (('tools',), 2), ((), 2)])
def test_headless_discovery_cannot_admit_an_unnamed_owner(tmp_path, arguments, code):
    import subprocess

    root = tmp_path / 'must-remain-absent'
    environment = LaunchRoot.environment(os.environ)
    environment.pop('AGENT_COMMS_THREAD', None)
    result = subprocess.run(
        (sys.executable, '-c', 'from agent_comms.execution.worker.__main__ import main; raise SystemExit(main())', *LaunchRoot.from_path(root).arguments(), *arguments),
        env=environment, capture_output=True, timeout=30,
    )
    assert result.returncode == code, result.stderr.decode()
    assert not root.exists()


async def test_unvalidated_configuration_refuses_before_child(tmp_path, pinned_native):
    missing = tmp_path / "missing-native"
    with pytest.raises(FileNotFoundError) as caught:
        [item async for item in one_turn_events(
            str(missing), (), "must not send", str(tmp_path), package=pinned_native)]
    assert caught.value.filename == str(missing)


async def test_headless_uses_existing_owner_runtime_and_owner_project(
        monkeypatch, native_runtime_package):
    # The real private boundary disallows volatile or symlinked roots.
    import tempfile

    from agent_comms.execution.acp import CommsClient
    from agent_comms.comms import wire

    with tempfile.TemporaryDirectory(prefix="l0b-headless-", dir="/var/tmp") as owned:
        base = Path(owned)
        project = base / "owner-project"
        caller_project = base / "caller-project"
        project.mkdir()
        caller_project.mkdir()
        # Headless execution requires a private bus root (worker.py); its owner prepares one.
        comms = prepare_native_comms(wire(base / "wire"))
        comms.registry.declare(NativeThread('headless', frozenset(), Checkout(project)))
        # A host only starts through its owner, which hands it the bound listener.
        monkeypatch.chdir(caller_project)
        monkeypatch.setenv("AGENT_COMMS_ROOT", str(comms.root))
        monkeypatch.setenv("AGENT_COMMS_AGENT_BIN", "pi")
        monkeypatch.setenv("AGENT_COMMS_AGENT_ARGS", "")
        monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "openrouter/z-ai/glm-5.3-flash")
        monkeypatch.setenv("PYTHONPATH", str(Path(events.__file__).parent.parent))
        for key in ("AGENT_COMMS_THREAD", "PI_PROMPT", "PI_PARENT_ID"):
            monkeypatch.delenv(key, raising=False)
        started, = await asyncio.to_thread(comms.owners.start_many, ("headless",))
        try:
            registered = comms.registry.require("headless")
            assert registered.require_process().pid == started.pid
            assert registered.worktree == Checkout(project)
            client = CommsClient(comms, auto_wake=False)
            try:
                initialized = await client.initialize(protocol_version=1)
                assert initialized.agent_capabilities.prompt_capabilities.image
                loaded = await client.load_session(str(project), "headless")
                configuration = next(update for update in decode_updates(loaded.field_meta)
                                     if isinstance(update, CoordinationChangedUpdate))
                assert configuration.thread == registered.incarnation
                assert configuration.wire_root == str(comms.root)
                response = await client.prompt(
                    "headless",
                    [TextContentBlock(type="text", text="!relay #all headless native route")],
                )
                assert response.stop_reason == "end_turn"
                assert [m.body for m in read_history(comms, "#all")] == [
                    "headless native route"
                ]
                assert not comms.registry.require("headless").executing
            finally:
                await client.shutdown()
        finally:
            await asyncio.to_thread(comms.owners.stop, "headless")
        assert not comms.registry.require("headless").process_alive
