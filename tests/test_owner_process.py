"""Real isolated owners: birth-bound launch, retirement and refusal to interrupt."""

from __future__ import annotations
from agent_comms.native_package import pinned_native_package

import asyncio
import os
import shlex
import sys
import time
from contextlib import AsyncExitStack
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.owner_lifecycle import NoRelease
from agent_comms.child_process import ObservedProcess, ParentedProcess
from agent_comms.comms import Comms
from agent_comms.config_options import ThinkingLevelConfigOption
from agent_comms.errors import RelationViolationError
from agent_comms.runtime import RuntimeProxy, socket_path
from agent_comms.threads import Thread
from agent_comms.turn_lease import ActiveTurn, NoTurn


def test_real_owner_start_restart_and_stop_preserve_thread(tmp_path: Path, monkeypatch) -> None:
    comms = Comms(tmp_path)
    comms.messaging.initialize_private_initial_protocol()
    declared = comms.registry.new_thread('worker', frozenset(), str(tmp_path), task='retained task')
    comms.registry.declare(declared)
    first = comms.owners.start(
        "worker", agent_bin="pi", agent_args=[]
    )
    owner = comms.registry.require("worker")
    try:
        deadline = time.monotonic() + 10
        while not socket_path(tmp_path, first.pid).exists():
            assert owner.process_alive, "Actual worker exited before runtime attach"
            assert time.monotonic() < deadline, "Actual worker failed to attach runtime"
            time.sleep(0.02)
        assert comms.owners.start("worker").pid == owner.require_process().pid
        assert not owner.process_identity.runs_here()
        assert owner.process_identity is not None
        from agent_comms.native_pi import NativePiUnavailable
        from agent_comms.owner_cutover import StoppedOwnerInstallation

        # A fresh lifecycle derives its package from the pin; the reviewed layout refuses another.
        import agent_comms.private_nk_entrypoint as private_nk_entrypoint

        with monkeypatch.context() as patch:
            patch.setattr(private_nk_entrypoint, "pinned_native_package", lambda: tmp_path / "other-package")
            mismatched = Comms(tmp_path)
            before = comms.registry.snapshot()
            with pytest.raises(NativePiUnavailable, match="Canonical native Pi package layout"):
                mismatched.owners.restart_owners(["worker"], agent_bin="pi", agent_args=[])
        assert owner.process_alive
        assert comms.registry.snapshot() == before
        assert isinstance(comms.owners.releases.get("worker"), NoRelease)
        restarted = comms.owners.restart_owners(
            ["worker"], agent_bin="pi", agent_args=[]
        )
        replacement = comms.registry.require("worker")
        assert len(restarted) == 1
        assert replacement.process_alive and not owner.process_alive
        assert replacement.process_identity != owner.process_identity
        assert replacement.created_at == declared.created_at
        assert replacement.task == declared.task

        class BindInstalledLaunch(StoppedOwnerInstallation):
            """A fresh lifecycle derives the installed launch itself."""

            def require_selection(self, snapshot, owners):
                assert [thread.name for thread in owners] == ["worker"]

            def after_stopped(self, lifecycle):
                assert not replacement.process_alive

        (installed,) = Comms(tmp_path).owners.restart_owners(
            ["worker"], agent_bin="pi", agent_args=[], cutover=BindInstalledLaunch()
        )
        assert installed.previous_pid == replacement.require_process().pid
        assert not replacement.process_alive
        replacement = comms.registry.require("worker")
        assert replacement.process_alive and replacement.require_process().pid == installed.pid
        comms.owners.stop("worker")
        assert not replacement.process_alive
        assert comms.registry.status("worker").stopped
        assert comms.registry.require("worker").created_at == declared.created_at
    finally:
        current = comms.registry.require("worker")
        if current.process_alive:
            assert current.process_identity is not None
            ObservedProcess(current.process_identity).stop_sync()


def test_stale_stored_birth_does_not_signal_real_process(tmp_path: Path) -> None:
    child = ParentedProcess.launch((sys.executable, "-c", "import time; time.sleep(60)"))
    try:
        comms = Comms(tmp_path)
        comms.registry.declare(
            comms.registry.new_thread('stale', frozenset(), str(tmp_path), process_identity=replace(child.identity, start_time=child.identity.start_time + 1))
        )
        with pytest.raises(RelationViolationError, match="another live owner"):
            comms.owners.restart_owners(["stale"])
        assert child.alive()
        comms.owners.stop("stale")
        assert child.alive()
        assert comms.registry.status("stale").stopped
    finally:
        child.stop_sync()


def test_restart_preflights_all_owners_before_signalling(tmp_path: Path) -> None:
    children = [
        ParentedProcess.launch((sys.executable, "-c", "import time; time.sleep(60)"))
        for _ in range(2)
    ]
    try:
        comms = Comms(tmp_path)
        for index, child in enumerate(children):
            comms.registry.declare(
                comms.registry.new_thread(f'worker-{index}', frozenset(), str(tmp_path), process_identity=child.identity, active_turn=ActiveTurn('busy', child.pid) if index else NoTurn())
            )
        with pytest.raises(RelationViolationError, match="idle before restart"):
            comms.owners.restart_owners(["worker-0", "worker-1"])
        assert all(child.alive() for child in children)
        assert all(comms.registry.status(f"worker-{i}").active for i in range(2))
    finally:
        for child in children:
            child.stop_sync()


def test_wait_graph_uses_exact_birth_for_real_active_peer(tmp_path: Path) -> None:
    from agent_comms.goal_presentation import GoalWaitTarget
    from agent_comms.goal_waits import GoalWaits

    child = ParentedProcess.launch((sys.executable, "-c", "import time; time.sleep(60)"))
    try:
        comms = Comms(tmp_path)
        owner = comms.registry.new_thread('owner', frozenset(), str(tmp_path))
        peer = comms.registry.new_thread('peer', frozenset(), str(tmp_path), process_identity=child.identity, active_turn=ActiveTurn('work', child.pid))
        comms.registry.declare(owner)
        comms.registry.declare(peer)
        targets = (GoalWaitTarget(peer.incarnation.require_recorded()),)
        assert GoalWaits.closed_wait_group(owner.name, targets, {}, comms.registry.snapshot(), comms.agents) == ()
        stale = replace(
            peer, process_identity=replace(child.identity, start_time=child.identity.start_time + 1)
        )
        comms.registry.register(stale)
        assert GoalWaits.closed_wait_group(owner.name, targets, {}, comms.registry.snapshot(), comms.agents) == (
            owner.name,
        )
        assert child.alive()
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
        assert owner.session_file is None
    finally:
        if owner.process_alive:
            ObservedProcess(owner.process_identity).stop_sync()


@pytest.mark.skipif(sys.platform != "linux", reason="Exact retained launch uses Linux /proc")
def test_real_batch_retains_each_launch_and_busy_refuses_every_stop(tmp_path, monkeypatch):
    """One provider-free installed worker batch, with original distinct settings."""
    from agent_comms.goal_states import BlockedGoal
    from agent_comms.goals import Goal
    from agent_comms.owner_launch import RetainedOwnerLaunch
    from agent_comms.owner_cutover import StoppedOwnerInstallation
    from agent_comms.private_bus_checkpoint import install_private_bus_checkpoint

    model = "openai-codex/gpt-6.1-sol"
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", model)
    comms = Comms(tmp_path)
    package = pinned_native_package()
    comms.messaging.initialize_private_initial_protocol()

    async def thinking_options():
        async with AsyncExitStack() as lifetime:
            # Query the fixture workers' pinned SDK, not the caller's launcher.
            client = CommsAgent(comms, agent_bin=str(package / 'dist' / 'cli.js'), auto_wake=False)
            lifetime.push_async_callback(client.shutdown)
            catalog = client.sessions.config.catalog_for(ThinkingLevelConfigOption)
            return await catalog.choices(comms.registry.new_thread('catalog', frozenset(), str(tmp_path), model=model))

    declared_options = asyncio.run(thinking_options())
    thinking = declared_options[0].value
    originals = []
    settings = (
        ("batch-a", ("--offline", "--no-tools", "--thinking", thinking), "credential-a"),
        ("batch-b", (), "credential-b"),
    )

    class RebuildFixtureCheckpoint(StoppedOwnerInstallation):
        """An actual declared writer operation; it never starts or stops owners."""

        def require_selection(self, snapshot, owners):
            live = {
                thread.name for thread in snapshot.threads.values()
                if thread.role.executes(snapshot.statuses[thread.name].active)
                and thread.process_alive
            }
            if {thread.name for thread in owners} != live:
                raise RelationViolationError("Checkpoint installation requires the complete batch")

        def after_stopped(self, lifecycle):
            assert all(not original.process_alive for original in originals)
            assert all(
                lifecycle.registry.require(name).process_identity == original.process_identity
                for name, original in zip(selected, originals, strict=True)
            )
            bus = lifecycle.bus.log
            # This fixture's original writer already understands its original
            # prefix record. Live old-schema ownership remains a separate cutover.
            with bus.locked():
                marker = bus.read_metadata_unlocked()
                before = replace(marker, checkpoint_version=None, checkpoint_seal=None)
                source = bus.path.read_bytes()
                checkpoint = lifecycle.root / "private_bus_checkpoint.sqlite3"
                checkpoint.rename(checkpoint.with_suffix(".retained-original"))
                bus.write_metadata_unlocked(before)
                install_private_bus_checkpoint(bus, _bus_locked=True)
                assert replace(
                    bus.read_metadata_unlocked(), checkpoint_version=None, checkpoint_seal=None
                ) == before
                assert bus.path.read_bytes() == source

    cutover = RebuildFixtureCheckpoint()

    def ready(owner):
        async def attach():
            # The old helper allowed 10 s for opening plus 10 s for ready.
            async with asyncio.timeout(20), AsyncExitStack() as lifetime:
                client = CommsAgent(comms, auto_wake=False)
                lifetime.push_async_callback(client.shutdown)
                proxy = RuntimeProxy(client, owner.name, socket_path(tmp_path, owner.require_process().pid))
                lifetime.push_async_callback(proxy.close)
                await proxy.subscribe()

        asyncio.run(attach())

    try:
        for name, arguments, credential in settings:
            goal = Goal("Protected failed original", name + "-goal", published_seq=0, state=BlockedGoal("No replay"))
            comms.registry.declare(comms.registry.new_thread(name, frozenset({name}), str(tmp_path), model=model, thinking_level=thinking, goal=goal, task='retained task ' + name))
            with monkeypatch.context() as patch:
                patch.setenv("BATCH_OWNER_CREDENTIAL", credential)
                patch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / (name + "-config")))
                comms.owners.start(name, agent_args=arguments)
            original = comms.registry.require(name)
            ready(original)
            originals.append(original)
        # The original process launch name remains valid through registry rename.
        comms.registry.rename("batch-b", "batch-renamed")
        selected = ["batch-a", "batch-renamed"]
        before = comms.registry.snapshot()
        last = before.require_active("batch-renamed")
        busy = replace(last, active_turn=ActiveTurn("protected-turn", last.require_process().pid))
        comms.registry.register(busy)
        refusal_source = comms.registry.snapshot()
        with pytest.raises(RelationViolationError, match="idle before restart"):
            comms.owners.restart_owners(selected, cutover=cutover)
        assert comms.registry.snapshot() == refusal_source
        assert all(owner.process_alive for owner in originals)
        comms.registry.register(last)
        with pytest.raises(RelationViolationError, match="complete batch"):
            comms.owners.restart_owners([selected[0]], cutover=cutover)
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
        prefix = target_runtime.interpreter.parent.parent
        target_binary = tmp_path / "target-pi-comms-native"
        target_binary.symlink_to(target_runtime.native_entrypoint)
        receipts = comms.owners.restart_owners(
            selected, agent_bin=str(target_binary), cutover=cutover
        )
        assert len(receipts) == 2
        assert all(not owner.process_alive for owner in originals)
        for index, (acknowledgement, (_, arguments, credential)) in enumerate(zip(receipts, settings, strict=True)):
            current = comms.registry.require(acknowledgement.thread)
            ready(current)
            launch = RetainedOwnerLaunch.capture(current, comms.registry.snapshot())
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
        for owner in comms.registry.snapshot().threads.values():
            if owner.role.executes(owner.process_alive):
                comms.owners.stop(owner.name)
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
