"""Actual queued owner handoff retains history and accepts a fresh native input."""

import asyncio
import json
import os
import select
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from acp import spawn_agent_process
from acp.schema import TextContentBlock

from agent_comms import restart_queue as queue
from agent_comms.comms import Comms
from agent_comms.child_process import AttachedChild
from agent_comms.field_codec import FieldCodec
from agent_comms.private_nk_entrypoint import ROOT_ID_ENV
from agent_comms.runtime import socket_path
from agent_comms.threads import Thread
from explicit_private_owner_installed_journey import Subscriber
from test_backend_native_lifecycle import native_backend


@pytest.mark.skipif(sys.platform != "linux", reason="Linux watcher")
def test_inotify_wakes_for_registry_and_queue_changes(tmp_path):
    root = tmp_path / "wire"
    root.mkdir()
    directory = root / queue.DIRECTORY
    directory.mkdir()
    with queue._watch(root, directory) as fd:
        (root / "registry.json").write_text("changed")
        assert select.select([fd], [], [], 1)[0] == [fd]
        os.read(fd, 65536)
        (directory / "next.json").write_text("queued")
        assert select.select([fd], [], [], 1)[0] == [fd]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux watcher")
async def test_actual_queued_restart_retains_history_and_accepts_new_input(
    native_backend, monkeypatch
):
    fixture = native_backend
    if not os.environ.get("QUEUE_SOURCE_RUNTIME") or not os.environ.get("QUEUE_SOURCE_PACKAGE"):
        pytest.skip(
            "Set QUEUE_SOURCE_RUNTIME and QUEUE_SOURCE_PACKAGE to retained reviewed source pins"
        )
    comms = Comms(fixture.root)
    comms.owners.pin_private_nk_launch(
        comms.root, os.environ[ROOT_ID_ENV], Path(os.environ["PI_COMPACTION_TEST_PACKAGE"])
    )
    comms.registry.declare(
        Thread(
            "worker",
            frozenset(),
            str(fixture.project),
            session_file=str(fixture.session),
            model="response-local/fixture",
            thinking_level="off",
        )
    )
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "response-local/fixture")
    monkeypatch.setenv(
        "AGENT_COMMS_AGENT_ARGS",
        "--offline --no-extensions --no-skills --no-context-files --no-tools",
    )
    start_watcher = queue._start_watcher
    watchers = []

    def retained_watcher(root, environment):
        watcher = start_watcher(root, environment)
        watchers.append(watcher)
        return watcher

    monkeypatch.setattr(queue, "_start_watcher", retained_watcher)
    # Reuse the real retained historical installation, with a distinct interpreter
    # and native bundle; no substitute worker or protocol fixture.
    source_runtime = Path(os.environ["QUEUE_SOURCE_RUNTIME"])
    source_package = Path(os.environ["QUEUE_SOURCE_PACKAGE"])
    target_interpreter = sys.executable
    target_package = Path(os.environ["PI_COMPACTION_TEST_PACKAGE"])
    source_environment = dict(os.environ)
    source_environment.pop("PYTHONPATH", None)
    source_environment.update(
        {
            "VIRTUAL_ENV": str(source_runtime),
            queue.PACKAGE_ENV: str(source_package),
            "QUEUE_FIXTURE_AUTH_SENTINEL": "source-auth-only",
        }
    )
    starter = await AttachedChild.start(
        (
            str(source_runtime / "bin/python"),
            "-m",
            "agent_comms.cli",
            "--root",
            str(comms.root),
            "invoke",
            "--tool",
            "comms_start",
            "--arguments",
            json.dumps({"name": "worker"}),
        ),
        env=source_environment,
        cwd=fixture.project,
    )
    try:
        async with asyncio.timeout(10):
            outcome = await starter.wait()
        assert outcome.successful, await starter.stderr.read()
    except BaseException:
        await asyncio.to_thread(comms.owners.stop, "worker")
        raise
    finally:
        await starter.stop()
    original = comms.registry.require("worker")
    owners = [original.process_identity]

    async def attach_and_prompt(text, interpreter=target_interpreter):
        subscriber = Subscriber()
        environment = dict(os.environ, AGENT_COMMS_ROOT=str(comms.root))
        if interpreter != target_interpreter:
            environment.pop("PYTHONPATH", None)
            environment[queue.PACKAGE_ENV] = str(source_package)
        async with spawn_agent_process(
            subscriber,
            interpreter,
            "-m",
            "agent_comms.acp",
            env=environment,
            cwd=fixture.project,
        ) as (connection, child):
            async with asyncio.timeout(25):
                await connection.initialize(protocol_version=1)
                await connection.load_session(
                    cwd=str(fixture.project), session_id="worker", mcp_servers=[]
                )
                await connection.prompt("worker", [TextContentBlock(type="text", text=text)])
            assert any(fixture.provider.text in str(update) for update in subscriber.updates)

    async def ready():
        async with asyncio.timeout(15):
            while not socket_path(comms.root, comms.registry.require("worker").pid).exists():
                assert comms.registry.require("worker").process_alive
                await asyncio.sleep(0.02)

    try:
        await ready()
        await attach_and_prompt("QUEUE_INITIAL_MARKER", str(source_runtime / "bin/python"))
        before = fixture.session.read_bytes()
        baseline_posts = fixture.provider.posts
        assert baseline_posts == 1
        entered, release = asyncio.Event(), asyncio.Event()
        provider_handle = fixture.provider.handle

        async def held_response(reader, writer):
            entered.set()
            await release.wait()
            await provider_handle(reader, writer)

        fixture.provider.handle = held_response
        active = asyncio.create_task(
            attach_and_prompt("BUSY_BEFORE_QUEUE_HANDOFF", str(source_runtime / "bin/python"))
        )
        try:
            await asyncio.wait_for(entered.wait(), 15)
            request = queue.enqueue(comms, "worker")
            assert request.state.pending and request.selection.process == original.process_identity
            assert request.source_interpreter == str(source_runtime / "bin/python")
            assert request.target.interpreter == target_interpreter
            assert request.target.runtime.package == str(target_package)
            assert request.target.agent_bin == str(
                Path(target_interpreter).with_name("pi-comms-native")
            )
            await asyncio.to_thread(queue.step, comms)
            assert queue.status(comms, "worker")[0].state.pending
            assert comms.registry.require("worker").process_identity == original.process_identity
        finally:
            release.set()
            await active
            fixture.provider.handle = provider_handle
        async with asyncio.timeout(20):
            while not isinstance(
                (receipt := queue.status(comms, "worker")[0]).state, queue.RestartedRestart
            ):
                assert receipt.state.active, receipt
                await asyncio.sleep(0.02)
        replacement = comms.registry.require("worker")
        owners.append(replacement.process_identity)
        assert receipt.state.previous == original.process_identity
        assert receipt.state.current == replacement.process_identity
        assert not original.process_alive
        await ready()
        assert replacement.session_file == str(fixture.session)
        assert (
            replacement.model == original.model
            and replacement.thinking_level == original.thinking_level
        )
        replacement_env = queue._owner_environment(replacement.pid, "worker", target_interpreter)
        assert replacement_env["QUEUE_FIXTURE_AUTH_SENTINEL"] == "source-auth-only"
        assert replacement_env["PI_CODING_AGENT_DIR"] == str(fixture.config)
        assert replacement_env[queue.PACKAGE_ENV] == str(target_package)
        assert replacement_env[queue.RestartEnvironment.binary_key] == request.target.agent_bin
        assert replacement_env.get("PYTHONPATH") == os.environ.get("PYTHONPATH")
        assert replacement_env.get("VIRTUAL_ENV") == os.environ.get("VIRTUAL_ENV")
        assert fixture.session.read_bytes().startswith(before)
        await attach_and_prompt("AFTER_QUEUE_NEW_INPUT")
        assert fixture.provider.posts == baseline_posts + 2
        messages = fixture.saved_inputs()
        assert len(messages) == 3
        assert len({message["inputId"] for message in messages}) == 3
        assert all(
            sum(text in str(message) for message in messages) == 1
            for text in (
                "QUEUE_INITIAL_MARKER",
                "BUSY_BEFORE_QUEUE_HANDOFF",
                "AFTER_QUEUE_NEW_INPUT",
            )
        )
        completed = queue.status(comms, "worker")[0]
        await asyncio.to_thread(queue.step, comms)
        assert queue.status(comms, "worker")[0] == completed
        assert comms.registry.require("worker").process_identity == replacement.process_identity
        raw = (comms.root / queue.DIRECTORY / f"{request.id}.json").read_text()
        assert "local-only" not in raw and "source-auth-only" not in raw
        assert FieldCodec.decode(queue.QueuedRestart, json.loads(raw)) == completed

        for watcher in watchers:
            await asyncio.wait_for(watcher.wait(), 5)
        # The remaining controls exercise actual queue transitions and owner
        # signals synchronously, with no unattended background watcher race.
        monkeypatch.setattr(queue, "_start_watcher", lambda root, environment: None)
        wrong_target = queue.enqueue(comms, "worker")
        wrong_target = replace(
            wrong_target,
            target=replace(
                wrong_target.target, agent_bin=str(source_runtime / "bin/pi-comms-native")
            ),
        )
        queue._save(comms.root / queue.DIRECTORY / f"{wrong_target.id}.json", wrong_target)
        await asyncio.to_thread(queue.step, comms)
        assert isinstance(
            next(row.state for row in queue.status(comms, "worker") if row.id == wrong_target.id),
            queue.BlockedRestart,
        )
        assert comms.registry.require("worker").process_identity == replacement.process_identity
        cancelled = queue.enqueue(comms, "worker")
        (cancelled,) = queue.cancel(comms, "worker")
        assert isinstance(cancelled.state, queue.CancelledRestart)
        await asyncio.to_thread(queue.step, comms)
        assert comms.registry.require("worker").process_identity == replacement.process_identity
        stale = queue.enqueue(comms, "worker")
        await asyncio.to_thread(comms.owners.restart_owners, ["worker"], expected=stale.selection)
        changed = comms.registry.require("worker")
        owners.append(changed.process_identity)
        await ready()
        await asyncio.to_thread(queue.step, comms)
        assert isinstance(
            next(row.state for row in queue.status(comms, "worker") if row.id == stale.id),
            queue.StaleRestart,
        )
        assert comms.registry.require("worker").process_identity == changed.process_identity
        uncertain = queue.enqueue(comms, "worker")
        before_uncertain = fixture.session.read_bytes()

        def fail_after_retirement(*args, **kwargs):
            raise RuntimeError("Fixture fault after retirement, before replacement launch")

        with monkeypatch.context() as patch:
            patch.setattr(comms.owners, "_launch_owner_unlocked", fail_after_retirement)
            await asyncio.to_thread(queue.step, comms)
        states = {row.id: row.state for row in queue.status(comms, "worker")}
        assert isinstance(states[uncertain.id], queue.UncertainRestart)
        assert not changed.process_alive
        await asyncio.to_thread(queue.step, comms)
        assert {row.id: row.state for row in queue.status(comms, "worker")} == states
        assert fixture.session.read_bytes() == before_uncertain
        assert fixture.provider.posts == baseline_posts + 2
    finally:
        await asyncio.to_thread(comms.owners.stop, "worker")
        for watcher in watchers:
            await watcher.stop()
        assert all(not identity.alive() for identity in owners)
        assert all(not watcher.alive() for watcher in watchers)
