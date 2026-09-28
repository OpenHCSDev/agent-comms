"""Retained native execution, refusal, and canonical headless/runtime behavior."""

import asyncio
import os
import sys
from pathlib import Path

import pytest

from agent_comms import agent_events as events
from agent_comms.backend import stream_agent_events
from agent_comms.child_process import BoundedRun
from agent_comms.native_pi import NativePiRpcLaunch, NativePiUnavailable
from agent_comms.private_nk_entrypoint import PrivateNkLaunch


def test_launcher_spelling_cannot_select_execution(tmp_path, monkeypatch):
    from agent_comms import native_pi, private_nk_entrypoint

    package = tmp_path / "package"
    route = PrivateNkLaunch(tmp_path, "a" * 32, package, None)
    monkeypatch.setattr(private_nk_entrypoint, "private_nk_from_environment", lambda: route)
    verified = []
    monkeypatch.setattr(native_pi, "_trusted_package", lambda value: verified.append(value))
    forged = tmp_path / "pi-comms-native"
    forged.write_text("#!/bin/sh\necho not-native\n")
    forged.chmod(0o755)
    with pytest.raises(NativePiUnavailable, match="validated native"):
        NativePiRpcLaunch.managed(str(forged), (), worktree=tmp_path)
    assert verified == []
    launch = NativePiRpcLaunch.managed(
        "pi",
        ("--provider", "local", "--model", "fixture"),
        worktree=tmp_path,
        environment={"NODE_OPTIONS": "--import=/untrusted.js", "PI_WORKTREE": "/wrong"},
    )
    assert verified == [package]
    assert launch.package == package
    assert "NODE_OPTIONS" not in launch.env
    assert launch.env["PI_WORKTREE"] == str(tmp_path)
    assert str(package / "dist/cli.js") in launch.argv
    assert "--provider" in launch.argv and "fixture" in launch.argv
    for arguments in (
        ("--mode", "text"),
        ("--mode=interactive",),
        ("--mode",),
        ("--print",),
        ("-p",),
        ("--help",),
        ("--version",),
    ):
        with pytest.raises(NativePiUnavailable):
            NativePiRpcLaunch.managed("pi", arguments, worktree=tmp_path)


async def test_unvalidated_configuration_refuses_before_child(tmp_path, monkeypatch):
    from agent_comms.child_process import AttachedChild

    async def forbidden(*args, **kwargs):
        raise AssertionError("Unvalidated configuration reached child execution")

    monkeypatch.setattr(AttachedChild, "start", forbidden)
    result = [
        item
        async for item in stream_agent_events(
            str(tmp_path / "missing-native"), (), "must not send", str(tmp_path)
        )
    ]
    assert len(result) == 1 and isinstance(result[0], events.Done)
    assert not result[0].ok and result[0].reason_code == "native_launch_invalid"
    assert "missing-native" in result[0].text


@pytest.mark.usefixtures("native_rpc_fixture")
async def test_native_early_exit_retains_failure_without_raw_stdout(tmp_path):
    child = tmp_path / "rpc-child"
    child.write_text(f"#!{sys.executable}\nimport sys\nprint('not a native record')\nsys.exit(3)\n")
    child.chmod(0o755)
    result = [item async for item in stream_agent_events(str(child), (), "task", str(tmp_path))]
    assert not result[-1].ok
    assert not any(isinstance(item, events.Chunk) for item in result)


async def test_actual_pinned_native_launch_preflight_without_prompt(tmp_path, monkeypatch):
    from agent_comms import private_nk_entrypoint
    from agent_comms.pi_commands import GetState
    from agent_comms.pi_events import Response
    from agent_comms.pi_rpc import PiRpcChannel

    package = os.environ.get("AC_NATIVE_COPIED_PACKAGE")
    if not package:
        pytest.skip("Prepared package is required; never build or contact a provider")
    monkeypatch.setattr(
        private_nk_entrypoint,
        "private_nk_from_environment",
        lambda: PrivateNkLaunch(
            tmp_path,
            "a" * 32,
            Path(package),
            None,
        ),
    )
    launch = NativePiRpcLaunch.managed(
        "pi",
        (
            "--no-extensions",
            "--no-skills",
            "--no-context-files",
            "--session-dir",
            str(tmp_path / "sessions"),
        ),
        worktree=tmp_path,
        environment={"PI_CODING_AGENT_DIR": str(tmp_path / "agent-config")},
    )
    async with BoundedRun.session(launch.argv, cwd=launch.cwd, env=launch.env, timeout=12) as child:
        channel = PiRpcChannel(child.stdout)
        child.stdin.write(channel.encode(GetState(id="preflight")))
        await child.stdin.drain()
        while event := await channel.receive(strict=True):
            if isinstance(event, Response) and event.id == "preflight":
                assert event.success
                assert event.data.native_input_proof_capability == "pi-native-input-v1-live-only"
                break
        else:
            raise AssertionError("No native preflight response")


async def test_headless_uses_existing_owner_runtime_and_owner_project(tmp_path, monkeypatch):
    from agent_comms.acp import CommsClient
    from agent_comms.comms import Comms
    from agent_comms.runtime import socket_path
    from agent_comms.threads import Thread

    package = os.environ.get("AC_NATIVE_COPIED_PACKAGE")
    if not package:
        pytest.skip("Prepared package is required; headless startup makes no provider request")
    # The real private boundary disallows volatile or symlinked roots.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="l0b-headless-", dir="/var/tmp") as owned:
        base = Path(owned)
        project = base / "owner-project"
        caller_project = base / "caller-project"
        project.mkdir()
        caller_project.mkdir()
        comms = Comms(base / "wire")
        root_id = comms.messaging.initialize_private_initial_protocol()
        comms.messaging.initialize_private_claim_protocol()
        comms.threads.register(Thread("headless", frozenset(), str(project)))
        env = dict(
            os.environ,
            AGENT_COMMS_ROOT=str(comms.root),
            AGENT_COMMS_THREAD="headless",
            AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=root_id,
            AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=package,
            AGENT_COMMS_AGENT_BIN="pi",
            AGENT_COMMS_AGENT_ARGS="",
            AGENT_COMMS_NO_REPLY_WINDOW="0.05",
            AGENT_COMMS_REPLY_WINDOW="0.1",
            AGENT_COMMS_REPLY_QUIET="0.05",
            PYTHONPATH=str(Path(__file__).parents[1] / "src"),
            AGENT_COMMS_AGENT_MODELS="openrouter/z-ai/glm-5.3-flash",
        )
        for key in ("PI_PROMPT", "PI_PARENT_ID", "PI_AGENT_ID"):
            env.pop(key, None)
        async with BoundedRun.session(
            (sys.executable, "-c", "from agent_comms.worker import main; raise SystemExit(main())"),
            cwd=caller_project,
            env=env,
            timeout=20,
        ) as child:
            async with asyncio.timeout(12):
                while not socket_path(comms.root, child.pid).exists():
                    if child.returncode is not None:
                        raise AssertionError((await child.stderr.read()).decode())
                    await asyncio.sleep(0.05)
            registered = comms.registry.require("headless")
            assert registered.pid == child.pid
            assert registered.worktree == str(project)
            client = CommsClient(
                comms, private_nk_wire_root_id=root_id, private_nk_native_package=Path(package)
            )
            try:
                loaded = await client.load_session(str(project), "headless")
                assert loaded.field_meta["agentComms"]["imagePrompts"]
                response = await client.prompt(
                    "headless", [{"type": "text", "text": "!relay #all headless native route"}]
                )
                assert response.stop_reason == "end_turn"
                assert [m.body for m in comms.views.channel_history("#all")] == [
                    "headless native route"
                ]
                assert comms.registry.require("headless").active_turn is None
            finally:
                await client.shutdown()
        assert not comms.registry.require("headless").process_alive
