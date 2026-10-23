"""Real isolated owners: birth-bound launch, retirement and refusal to interrupt."""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
import time
from contextlib import AsyncExitStack
from dataclasses import replace
from functools import partial
from pathlib import Path

import pytest

from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.owner_lifecycle import NoRelease
from agent_comms.child_process import ObservedProcess, SpawnedProcess
from agent_comms.comms import Comms
from agent_comms.config_options import ThinkingLevelConfigOption
from agent_comms.coordinator import Coordination
from agent_comms.field_codec import compile_decoder
from agent_comms.pi_vocabulary import ThinkingLevelField
from agent_comms.errors import RelationViolationError
from agent_comms.runtime import RuntimeProxy, socket_path
from agent_comms.launch_root import LaunchRoot
from agent_comms.native_arguments import GivenArguments, GivenBinary
from agent_comms.restart_scope import OwnersNamed
from agent_comms.thread_model import DeclaredModel
from agent_comms.thread_text import GivenThreadText
from agent_comms.session_file import NoSessionFile
from delivery_owner_fixture import canonical_agent
from native_backend_fixture import native_backend_fixture


@pytest.fixture
async def native_workers(tmp_path, native_runtime_package, source_programs):
    source_programs.bind(tmp_path)
    async with native_backend_fixture(tmp_path, package=native_runtime_package) as native:
        with LaunchRoot.from_path(native.root).selected():
            yield native


async def test_real_owner_start_restart_and_stop_preserve_thread(tmp_path: Path, native_workers) -> None:
    comms = Comms(native_workers.root)
    declared = comms.registry.new_thread('worker', frozenset(), str(tmp_path), task=GivenThreadText('retained task'))
    comms.registry.declare(declared)
    first = await Coordination.run_worker(partial(
        comms.owners.start, "worker", agent_bin=GivenBinary("pi"), agent_args=GivenArguments(()),
    ))
    owner = comms.registry.require("worker")
    try:
        deadline = time.monotonic() + 10
        while not socket_path(comms.root, first.pid).exists():
            assert owner.process_alive, "Actual worker exited before runtime attach"
            assert time.monotonic() < deadline, "Actual worker failed to attach runtime"
            await asyncio.sleep(0.02)
        assert (await Coordination.run_worker(partial(comms.owners.start, "worker"))).pid == owner.require_process().pid
        assert not owner.process_identity.runs_here()
        assert owner.process_identity is not None
        from agent_comms.owner_cutover import PreserveOwnerRuntime

        # The current launch derives its immutable package from the installation;
        # the removed mutable-package override is not a supported input.
        assert owner.process_alive
        assert isinstance(comms.owners.releases.get("worker"), NoRelease)
        restarted = await Coordination.run_worker(partial(
            comms.owners.restart_owners, OwnersNamed(("worker",)),
            agent_bin=GivenBinary("pi"), agent_args=GivenArguments(()),
        ))
        replacement = comms.registry.require("worker")
        assert len(restarted) == 1
        assert replacement.process_alive and not owner.process_alive
        assert replacement.process_identity != owner.process_identity
        assert replacement.created_at == declared.created_at
        assert replacement.task == declared.task

        class KeepFixtureRuntime(PreserveOwnerRuntime):
            """A fresh lifecycle retains its selected fixture runtime."""

            def require_selection(self, snapshot, owners):
                assert [thread.name for thread in owners] == ["worker"]

            def after_stopped(self, lifecycle):
                assert not replacement.process_alive

        (installed,) = await Coordination.run_worker(partial(
            Comms(comms.root).owners.restart_owners,
            OwnersNamed(("worker",)), agent_bin=GivenBinary("pi"),
            agent_args=GivenArguments(()), cutover=KeepFixtureRuntime(),
        ))
        assert installed.previous_pid == replacement.require_process().pid
        assert not replacement.process_alive
        replacement = comms.registry.require("worker")
        assert replacement.process_alive and replacement.require_process().pid == installed.pid
        await Coordination.run_worker(partial(comms.owners.stop, "worker"))
        assert not replacement.process_alive
        assert comms.registry.status("worker").stopped
        assert comms.registry.require("worker").created_at == declared.created_at
    finally:
        current = comms.registry.require("worker")
        if current.process_alive:
            assert current.process_identity is not None
            ObservedProcess(current.process_identity).stop_sync()


def test_stale_stored_birth_does_not_signal_real_process(tmp_path: Path) -> None:
    child = SpawnedProcess.launch((sys.executable, "-c", "import time; time.sleep(60)"))
    try:
        comms = Comms(tmp_path)
        comms.registry.declare(
            comms.registry.new_thread('stale', frozenset(), str(tmp_path), process_identity=replace(child.identity, start_time=child.identity.start_time + 1))
        )
        with pytest.raises(RelationViolationError):
            comms.owners.restart_owners(OwnersNamed(("stale",)))
        assert child.alive()
        comms.owners.stop("stale")
        assert child.alive()
        assert comms.registry.status("stale").stopped
    finally:
        child.stop_sync()






def test_failed_real_worker_startup_retains_private_trace(tmp_path: Path, monkeypatch) -> None:
    """The production detached launch retains a failure before socket creation."""
    comms = Comms(tmp_path)
    comms.registry.declare(comms.registry.new_thread('failed-start', frozenset(), str(tmp_path)))
    result = comms.owners.start("failed-start")
    owner = comms.registry.require("failed-start")
    deadline = time.monotonic() + 5
    try:
        while owner.process_alive:
            assert time.monotonic() < deadline, "Worker did not terminate on invalid launch"
            time.sleep(.01)
        logs = tuple((tmp_path / "diagnostics").glob("owner-*.log"))
        assert len(logs) == 1
        trace = logs[0].read_text()
        assert "Owner launch: failed-start" in trace
        assert "Traceback (most recent call last)" in trace
        assert "PublicationActivationBlocked" in trace
        assert logs[0].stat().st_mode & 0o777 == 0o600
        assert not socket_path(tmp_path, result.pid).exists()
        assert owner.session_file == NoSessionFile()
    finally:
        if owner.process_alive:
            ObservedProcess(owner.process_identity).stop_sync()


@pytest.mark.skipif(sys.platform != "linux", reason="Exact retained launch uses Linux /proc")
async def test_real_batch_retains_each_launch_and_busy_refuses_every_stop(tmp_path, monkeypatch, native_workers):
    """One provider-free installed worker batch, with original distinct settings."""
    from agent_comms.goal_states import BlockedGoal
    from agent_comms.goals import Goal
    from agent_comms.owner_launch import RetainedOwnerLaunch
    from agent_comms.owner_cutover import PreserveOwnerRuntime

    model = 'response-local/fixture'
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", model)
    comms = Comms(native_workers.root)
    package = native_workers.package.path

    async def thinking_options():
        async with AsyncExitStack() as lifetime:
            # Query the fixture workers' pinned SDK, not the caller's launcher.
            client = canonical_agent(comms, agent_bin=GivenBinary(str(package / 'dist' / 'cli.js')), auto_wake=False)
            lifetime.push_async_callback(client.shutdown)
            catalog = client.sessions.config.catalog_for(ThinkingLevelConfigOption)
            return await catalog.choices(comms.registry.new_thread('catalog', frozenset(), str(tmp_path), model=DeclaredModel(model)))

    declared_options = await thinking_options()
    thinking = compile_decoder(ThinkingLevelField).decode(declared_options[0].value)
    originals = []
    settings = (
        ("batch-a", ("--offline", "--no-tools", "--thinking", thinking.wire_value()), "credential-a"),
        ("batch-b", (), "credential-b"),
    )

    class RetainStoppedBatch(PreserveOwnerRuntime):
        """Check the complete stopped batch without modifying its retained data."""

        def require_selection(self, snapshot, owners):
            live = {
                thread.name for thread in snapshot.threads.values()
                if thread.restart_candidates(snapshot.status(thread.name))
            }
            if {thread.name for thread in owners} != live:
                raise RelationViolationError("Restart requires the complete batch")

        def after_stopped(self, lifecycle):
            assert all(not original.process_alive for original in originals)
            assert all(
                lifecycle.registry.require(name).process_identity == original.process_identity
                for name, original in zip(selected, originals, strict=True)
            )

    cutover = RetainStoppedBatch()

    async def ready(owner):
        async def attach():
            # The old helper allowed 10 s for opening plus 10 s for ready.
            async with asyncio.timeout(20), AsyncExitStack() as lifetime:
                client = canonical_agent(comms, auto_wake=False)
                lifetime.push_async_callback(client.shutdown)
                proxy = RuntimeProxy(client, owner.name, socket_path(comms.root, owner.require_process().pid))
                lifetime.push_async_callback(proxy.close)
                await proxy.subscribe()

        await attach()

    try:
        for name, arguments, credential in settings:
            goal = Goal("Protected failed original", name + "-goal", published_seq=0, state=BlockedGoal("No replay"))
            comms.registry.declare(comms.registry.new_thread(name, frozenset({name}), str(tmp_path), model=DeclaredModel(model), thinking_level=thinking, goal=goal, task=GivenThreadText('retained task ' + name)))
            with monkeypatch.context() as patch:
                patch.setenv("BATCH_OWNER_CREDENTIAL", credential)
                patch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / (name + "-config")))
                await Coordination.run_worker(partial(comms.owners.start, name, agent_args=GivenArguments(tuple(arguments))))
            original = comms.registry.require(name)
            await ready(original)
            originals.append(original)
        # The original process launch name remains valid through registry rename.
        await Coordination.run_worker(partial(
            comms.threads.rename_managed_thread, "batch-b", "batch-renamed",
            owner=originals[-1].require_process(),
        ))
        selected = ["batch-a", "batch-renamed"]
        before = comms.registry.snapshot()
        last = before.require_active("batch-renamed")
        # Seed the foreign owner's busy record through its owning edit, without
        # pretending this fixture process is that owner's local caller.
        with comms.registry.store.editing() as edit:
            busy, _ = edit.lease_turn(last, "protected-turn", None)
            edit.commit()
        refusal_source = comms.registry.snapshot()
        with pytest.raises(RelationViolationError):
            await Coordination.run_worker(partial(comms.owners.restart_owners, OwnersNamed(tuple(selected)), cutover=cutover))
        assert comms.registry.snapshot() == refusal_source
        assert all(owner.process_alive for owner in originals)
        assert comms.agents.finish_turn(busy.turn_lease)
        with pytest.raises(RelationViolationError):
            await Coordination.run_worker(partial(comms.owners.restart_owners, OwnersNamed((selected[0],)), cutover=cutover))
        assert all(original.process_alive for original in originals)
        original_goals = {name: comms.registry.require(name).goal for name in selected}
        source_files = {
            path: path.read_bytes() for path in tmp_path.rglob("*")
            if path.is_file() and path.suffix == ".jsonl"
        }
        # The operator's model/arguments/credential must never overwrite a source owner.
        monkeypatch.setenv("AGENT_COMMS_AGENT_ARGS", shlex.join(("--thinking", declared_options[-1].value)))
        monkeypatch.setenv("BATCH_OWNER_CREDENTIAL", "operator-only")
        target_runtime = LaunchRuntime.running()
        target_binary = tmp_path / "target-pi-comms-native"
        target_binary.symlink_to(target_runtime.native_entrypoint)
        replacements = await Coordination.run_worker(partial(
            comms.owners.restart_owners, OwnersNamed(tuple(selected)), agent_bin=GivenBinary(str(target_binary)), cutover=cutover,
        ))
        assert len(replacements) == 2
        assert all(not owner.process_alive for owner in originals)
        for index, (acknowledgement, (_, arguments, credential)) in enumerate(zip(replacements, settings, strict=True)):
            current = comms.registry.require(acknowledgement.thread)
            await ready(current)
            from agent_comms.owner_lifecycle import OwnerRestartSelection

            launch = RetainedOwnerLaunch.capture(
                OwnerRestartSelection.capture(comms.registry.snapshot(), current.name),
                comms.registry.snapshot())
            assert launch.binary == str(target_binary.resolve())
            assert launch.arguments == arguments
            assert launch.environment["BATCH_OWNER_CREDENTIAL"] == credential
            assert launch.environment["PI_CODING_AGENT_DIR"] == str(tmp_path / (settings[index][0] + "-config"))
            assert launch.environment["AGENT_COMMS_THREAD"] == selected[index]
            assert current.process_identity != originals[index].process_identity
            assert current.created_at == originals[index].created_at
            assert current.tags == originals[index].tags
            assert current.task == originals[index].task
            assert current.model == originals[index].model
            assert current.thinking_level == originals[index].thinking_level
            assert current.goal == original_goals[current.name]
            # Subscription may still be retiring metadata-only RPC children.
            # No child may select or persist a native conversation.
            children = Path(f"/proc/{current.require_process().pid}/task/{current.require_process().pid}/children").read_text().split()
            for child_pid in children:
                try:
                    arguments = Path(f"/proc/{child_pid}/cmdline").read_bytes().split(b"\0")
                except FileNotFoundError:
                    continue  # The metadata child finished retiring.
                if os.fsencode(package / "dist" / "cli.js") in arguments:
                    assert b"--no-session" in arguments, arguments
        assert all(path.read_bytes() == content for path, content in source_files.items())
        assert not list(tmp_path.rglob("*.input-proof"))

    finally:
        snapshot = comms.registry.snapshot()
        for owner in snapshot.threads.values():
            if owner.restart_candidates(snapshot.status(owner.name)):
                await Coordination.run_worker(partial(comms.owners.stop, owner.name))
        assert all(not owner.process_alive for owner in originals)


def test_native_configuration_retains_original_home_across_writable_fork(tmp_path, monkeypatch):
    from agent_comms.launch_runtime import RestartEnvironment

    original = tmp_path / "original-home"
    canonical = original / "credentials"
    canonical.mkdir(parents=True)
    auth = canonical / "auth.json"
    auth.write_text("original credentials")
    configuration = RestartEnvironment.inherit({
        "HOME": str(original), "PI_CODING_AGENT_DIR": "~/agent",
        "AGENT_COMMS_NATIVE_CONFIG_DIR": "~/credentials",
    })
    revision = configuration.auth_revision()
    fork = configuration.for_agent(tmp_path / "fork-policy")
    monkeypatch.setenv("HOME", str(tmp_path / "different-home"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "different-agent"))
    assert fork.native_config == canonical
    assert fork.agent_directory == tmp_path / "fork-policy"
    assert fork.auth_revision() == revision
    assert fork.encode_native() == {
        "AGENT_COMMS_NATIVE_CONFIG_DIR": str(canonical),
        "PI_CODING_AGENT_DIR": str(tmp_path / "fork-policy"),
    }
    assert auth.read_text() == "original credentials"


def test_native_configuration_uses_original_pi_directory_when_not_explicit(tmp_path):
    from agent_comms.launch_runtime import RestartEnvironment

    configuration = RestartEnvironment.inherit({
        "HOME": str(tmp_path), "PI_CODING_AGENT_DIR": "~/configured-agent",
    })
    assert configuration.native_config == configuration.agent_directory == tmp_path / "configured-agent"
    assert configuration.for_agent(tmp_path / "output").native_config == tmp_path / "configured-agent"
