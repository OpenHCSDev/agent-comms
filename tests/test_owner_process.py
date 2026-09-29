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
