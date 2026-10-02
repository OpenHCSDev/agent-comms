"""Real isolated owners: birth-bound launch, retirement and refusal to interrupt."""

from __future__ import annotations

import os
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.child_process import ObservedProcess, ParentedProcess
from agent_comms.comms import Comms
from agent_comms.errors import RelationViolationError
from agent_comms.runtime import socket_path
from agent_comms.threads import Thread
from agent_comms.turn_lease import ActiveTurn


def test_real_owner_start_restart_and_stop_preserve_thread(tmp_path: Path) -> None:
    comms = Comms(tmp_path)
    package = Path(os.environ["PI_COMPACTION_TEST_PACKAGE"])
    root_id = comms.messaging.initialize_private_initial_protocol()
    comms.owners.pin_private_nk_launch(tmp_path, root_id, package)
    declared = Thread("worker", frozenset(), str(tmp_path), task="retained task")
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
        assert comms.owners.start("worker").pid == owner.pid
        assert owner.pid != os.getpid()
        assert owner.process_identity is not None
        restarted = comms.owners.restart_owners(
            ["worker"], agent_bin="pi", agent_args=[]
        )
        replacement = comms.registry.require("worker")
        assert len(restarted) == 1
        assert replacement.process_alive and not owner.process_alive
        assert replacement.process_identity != owner.process_identity
        assert replacement.created_at == declared.created_at
        assert replacement.task == declared.task
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
            Thread(
                "stale",
                frozenset(),
                str(tmp_path),
                process_identity=replace(child.identity, start_time=child.identity.start_time + 1),
            )
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
                Thread(
                    f"worker-{index}",
                    frozenset(),
                    str(tmp_path),
                    process_identity=child.identity,
                    active_turn=ActiveTurn("busy", child.pid) if index else None,
                )
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
        owner = Thread("owner", frozenset(), str(tmp_path))
        peer = Thread(
            "peer",
            frozenset(),
            str(tmp_path),
            process_identity=child.identity,
            active_turn=ActiveTurn("work", child.pid),
        )
        comms.registry.declare(owner)
        comms.registry.declare(peer)
        targets = (GoalWaitTarget(peer.name, peer.created_at),)
        assert GoalWaits.closed_wait_group(owner.name, targets, {}, comms.registry.snapshot()) == ()
        stale = replace(
            peer, process_identity=replace(child.identity, start_time=child.identity.start_time + 1)
        )
        comms.registry.register(stale)
        assert GoalWaits.closed_wait_group(owner.name, targets, {}, comms.registry.snapshot()) == (
            owner.name,
        )
        assert child.alive()
    finally:
        child.stop_sync()


def test_failed_real_worker_startup_retains_private_trace(tmp_path: Path, monkeypatch) -> None:
    """The production detached launch retains a failure before socket creation."""
    monkeypatch.setenv("AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID", "invalid")
    monkeypatch.setenv("AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE", "/missing/native")
    comms = Comms(tmp_path)
    comms.registry.declare(Thread("failed-start", frozenset(), str(tmp_path)))
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

    comms = Comms(tmp_path)
    package = Path(os.environ["PI_COMPACTION_TEST_PACKAGE"])
    root_id = comms.messaging.initialize_private_initial_protocol()
    comms.owners.pin_private_nk_launch(tmp_path, root_id, package)
    originals = []
    settings = (
        ("batch-a", ("--offline", "--no-tools", "--thinking", "off"), "credential-a"),
        ("batch-b", (), "credential-b"),
    )

    class RebuildFixtureCheckpoint(StoppedOwnerInstallation):
        """An actual declared writer operation; it never starts or stops owners."""

        def require_selection(self, snapshot, owners):
            live = {
                thread.name for thread in snapshot.threads.values()
                if thread.role.executable and snapshot.statuses[thread.name].active
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
            # certificate. Live old-schema custody remains a separate cutover.
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
        deadline = time.monotonic() + 10
        while not socket_path(tmp_path, owner.pid).exists():
            assert owner.process_alive, "Actual batch worker exited before runtime attach"
            assert time.monotonic() < deadline, "Actual batch worker failed to attach"
            time.sleep(.02)
        # Socket creation precedes the owner's saved-history replay. Wait for
        # the existing protocol's complete attachment, so rename cannot race
        # the original startup read and silently turn the batch into one owner.
        import asyncio
        import json
        from agent_comms.runtime_requests import SubscribeRuntimeRequest

        async def attach():
            async with asyncio.timeout(10):
                reader, writer = await asyncio.open_unix_connection(
                    socket_path(tmp_path, owner.pid), limit=8 * 1024 * 1024
                )
                try:
                    writer.write((json.dumps(SubscribeRuntimeRequest(
                        thread=owner.name).to_wire()) + "\n").encode())
                    await writer.drain()
                    while line := await reader.readline():
                        response = json.loads(line)
                        assert "error" not in response, response
                        if "ready" in response:
                            return
                    raise AssertionError("Actual worker closed before complete attachment")
                finally:
                    writer.close()
                    await writer.wait_closed()

        asyncio.run(attach())

    try:
        for name, arguments, credential in settings:
            goal = Goal("Protected failed original", name + "-goal", state=BlockedGoal("No replay"))
            comms.registry.declare(Thread(
                name, frozenset({name}), str(tmp_path),
                model="openai-codex/gpt-6.1-sol", thinking_level="off",
                goal=goal, task="retained task " + name,
            ))
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
        busy = replace(last, active_turn=ActiveTurn("protected-turn", last.pid))
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
        monkeypatch.setenv("AGENT_COMMS_AGENT_ARGS", "--thinking high")
        monkeypatch.setenv("BATCH_OWNER_CREDENTIAL", "operator-only")
        target_binary = tmp_path / "target-pi-comms-native"
        target_binary.symlink_to(comms.owners.native_entrypoint())
        receipts = comms.owners.restart_owners(
            selected, agent_bin=str(target_binary), cutover=cutover
        )
        assert len(receipts) == 2
        assert all(not owner.process_alive for owner in originals)
        for index, (receipt, (_, arguments, credential)) in enumerate(zip(receipts, settings, strict=True)):
            current = comms.registry.require(receipt.thread)
            ready(current)
            launch = RetainedOwnerLaunch.capture(current, comms.registry.snapshot())
            assert launch.binary == str(target_binary)
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
            # Idle startup/handoff creates no model process and sends no original.
            assert not Path(f"/proc/{current.pid}/task/{current.pid}/children").read_text().strip()
        assert all(path.read_bytes() == content for path, content in source_files.items())
        assert not list(tmp_path.rglob("*.input-proof"))
    finally:
        for name in ("batch-a", "batch-renamed", "batch-b"):
            try:
                owner = comms.registry.require(name)
            except KeyError:
                continue
            if owner.process_alive:
                comms.owners.stop(owner.name)
        assert all(not owner.process_alive for owner in originals)


def test_native_configuration_retains_original_home_across_writable_fork(tmp_path, monkeypatch):
    from agent_comms.owner_launch import RestartEnvironment

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
    assert str(canonical / "models.json") in fork.settings_paths(tmp_path)
    assert str(tmp_path / "fork-policy/settings.json") in fork.settings_paths(tmp_path)
    assert auth.read_text() == "original credentials"


def test_native_configuration_uses_original_pi_directory_when_not_explicit(tmp_path):
    from agent_comms.owner_launch import RestartEnvironment

    configuration = RestartEnvironment.inherit({
        "HOME": str(tmp_path), "PI_CODING_AGENT_DIR": "~/configured-agent",
    })
    assert configuration.native_config == configuration.agent_directory == tmp_path / "configured-agent"
    assert configuration.for_agent(tmp_path / "output").native_config == tmp_path / "configured-agent"
