"""Retained native execution, refusal, and canonical headless/runtime behavior."""

from bus_history import read_history
import asyncio
import os
import sys
from pathlib import Path

import pytest

import agent_comms.agent_events as events
from agent_comms.child_process import BoundedRun
from agent_comms.launch_root import LaunchRoot
from agent_comms.acp_extension import CoordinationChangedUpdate, decode_updates
from pi_session_turn import one_turn_events


@pytest.mark.parametrize("arguments,code", [(('--help',), 0), (('tools',), 2), ((), 1)])
def test_headless_discovery_cannot_admit_an_unnamed_owner(tmp_path, arguments, code):
    import subprocess

    root = tmp_path / 'must-remain-absent'
    environment = LaunchRoot.environment(os.environ)
    environment.pop('AGENT_COMMS_THREAD', None)
    result = subprocess.run(
        (sys.executable, '-c', 'from agent_comms.worker import main; raise SystemExit(main())', *LaunchRoot.from_path(root).arguments(), *arguments),
        env=environment, capture_output=True, timeout=30,
    )
    assert result.returncode == code, result.stderr.decode()
    assert not root.exists()


async def test_unvalidated_configuration_refuses_before_child(tmp_path):
    missing = tmp_path / "missing-native"
    with pytest.raises(FileNotFoundError) as caught:
        [item async for item in one_turn_events(str(missing), (), "must not send", str(tmp_path))]
    assert caught.value.filename == str(missing)


async def test_headless_uses_existing_owner_runtime_and_owner_project(tmp_path, native_runtime_package):
    from agent_comms.acp import CommsClient
    from agent_comms.comms import Comms
    from agent_comms.runtime import socket_path

    # The real private boundary disallows volatile or symlinked roots.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="l0b-headless-", dir="/var/tmp") as owned:
        base = Path(owned)
        project = base / "owner-project"
        caller_project = base / "caller-project"
        project.mkdir()
        caller_project.mkdir()
        comms = Comms(base / "wire")
        comms.messaging.initialize_private_initial_protocol()
        comms.registry.declare(comms.registry.new_thread('headless', frozenset(), str(project)))
        env = dict(
            os.environ,
            AGENT_COMMS_ROOT=str(comms.root),
            AGENT_COMMS_THREAD="headless",
            AGENT_COMMS_AGENT_BIN="pi",
            AGENT_COMMS_AGENT_ARGS="",
            PYTHONPATH=str(Path(events.__file__).parent.parent),
            AGENT_COMMS_AGENT_MODELS="openrouter/z-ai/glm-5.3-flash",
        )
        for key in ("PI_PROMPT", "PI_PARENT_ID"):
            env.pop(key, None)
        async with BoundedRun.session(
            (sys.executable, "-c", "from agent_comms.worker import main; raise SystemExit(main())",
             *LaunchRoot.from_path(comms.root).arguments()),
            cwd=caller_project,
            env=env,
            timeout=20,
        ) as child:
            async with asyncio.timeout(12):
                while not socket_path(comms.root, child.pid).exists():
                    if child.returncode is not None:
                        raise AssertionError(await child.stderr_tail())
                    await asyncio.sleep(0.05)
            registered = comms.registry.require("headless")
            assert registered.require_process() == child.identity
            assert registered.worktree == str(project)
            client = CommsClient(comms)
            try:
                initialized = await client.initialize(protocol_version=1)
                assert initialized.agent_capabilities.prompt_capabilities.image
                loaded = await client.load_session(str(project), "headless")
                configuration = next(update for update in decode_updates(loaded.field_meta)
                                     if isinstance(update, CoordinationChangedUpdate))
                assert configuration.thread == registered.incarnation
                assert configuration.wire_root == str(comms.root)
                response = await client.prompt(
                    "headless", [{"type": "text", "text": "!relay #all headless native route"}]
                )
                assert response.stop_reason == "end_turn"
                assert [m.body for m in read_history(comms, "#all")] == [
                    "headless native route"
                ]
                assert not comms.registry.require("headless").executing
            finally:
                await client.shutdown()
        assert not comms.registry.require("headless").process_alive
