"""Operator restart preflights the whole selection and preserves thread state."""

import asyncio
import json
import os
import sys
from dataclasses import replace

import pytest

from agent_comms import Thread, wire
from agent_comms.cli import main
from agent_comms.declarations import ActiveTurn, RelationViolationError, ThreadStatus


def setup_owners(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    for index, name in enumerate(("one", "two", "stopped"), 1):
        comms.register(
            Thread(
                name,
                frozenset({"team"}),
                str(tmp_path),
                pid=10000 + index,
                session_file=f"/{name}.jsonl",
                model="test/model",
            )
        )
    comms.registry.unregister("stopped")
    monkeypatch.setattr(comms, "_process_alive", lambda pid: pid > 0)
    monkeypatch.setattr(comms, "_is_local_participant", lambda thread, wait=True: True)
    stopped = []

    def signal(pid, _signal):
        stopped.append({10001: "one", 10002: "two"}[pid])

    def launch(thread, agent_bin, agent_args):
        assert agent_bin == "pi"
        updated = replace(thread, pid=thread.pid + 1000)
        comms.registry.register(updated, new_owner=True)
        return updated

    monkeypatch.setattr(comms, "_signal_local_owner", signal)
    monkeypatch.setattr(comms, "_wait_for_owner_exit", lambda pid, timeout: True)
    monkeypatch.setattr(comms, "_launch_owner_unlocked", launch)
    return comms, stopped


def test_bulk_restart_preserves_state_and_does_not_revive_stopped(tmp_path, monkeypatch):
    comms, stopped = setup_owners(tmp_path, monkeypatch)
    old = comms.registry.require("one")
    results = comms.restart_owners()
    assert stopped == ["one", "two"]
    assert [receipt.thread for receipt in results] == stopped
    assert results[0].previous_pid == old.pid
    assert comms.registry.require("one") == replace(old, pid=old.pid + 1000)
    assert comms.registry.status("stopped").value == "stopped"


def test_restart_requires_exact_queued_owner_incarnation(tmp_path, monkeypatch):
    comms, stopped = setup_owners(tmp_path, monkeypatch)
    live = {10001, 10002}
    monkeypatch.setattr(comms, "_process_alive", lambda pid: pid in live)
    original_signal = comms._signal_local_owner

    def signal_and_exit(pid, sig):
        original_signal(pid, sig)
        live.discard(pid)

    monkeypatch.setattr(comms, "_signal_local_owner", signal_and_exit)
    original_launch = comms._launch_owner_unlocked

    def launch_and_live(thread, agent_bin, agent_args):
        updated = original_launch(thread, agent_bin, agent_args)
        live.add(updated.pid)
        return updated

    monkeypatch.setattr(comms, "_launch_owner_unlocked", launch_and_live)
    snapshot = comms.registry.snapshot()
    owner = snapshot.threads["one"]
    expected = (owner.pid, owner.created_at, snapshot.admission_generations["one"])
    with pytest.raises(ValueError, match="Queued owner incarnation changed"):
        comms.restart_owners(
            ["one"], expected_incarnations={"one": (expected[0], expected[1], expected[2] - 1)}
        )
    assert stopped == []
    (result,) = comms.restart_owners(["one"], expected_incarnations={"one": expected})
    assert result.previous_pid == expected[0]
    assert stopped == ["one"]
    with pytest.raises(ValueError, match="Queued owner incarnation changed"):
        comms.restart_owners(["one"], expected_incarnations={"one": expected})
    assert stopped == ["one"]


def test_guarded_restart_fences_post_signal_wake_before_exit(tmp_path, monkeypatch):
    comms, _ = setup_owners(tmp_path, monkeypatch)
    snapshot = comms.registry.snapshot()
    original = snapshot.threads["one"]
    expected = (original.pid, original.created_at, snapshot.admission_generations["one"])
    live = {original.pid}
    monkeypatch.setattr(comms, "_process_alive", lambda pid: pid in live)
    monkeypatch.setattr(comms, "_signal_local_owner", lambda pid, signal: live.discard(pid))
    observed = []

    def wait_after_signal(pid, timeout):
        # Simulate the old process receiving a wake immediately after signal
        # and before owner exit; it must fail the persisted admission gate.
        assert comms.registry.status("one") is ThreadStatus.STOPPED
        with monkeypatch.context() as patch:
            patch.setattr("agent_comms.declarations.os.getpid", lambda: original.pid)
            with pytest.raises(RelationViolationError, match="stopped or unavailable"):
                comms.registry.claim_local_turn("one", "post-signal-wake")
        observed.append(comms.registry.require("one").active_turn)
        return True

    monkeypatch.setattr(comms, "_wait_for_owner_exit", wait_after_signal)
    (result,) = comms.restart_owners(["one"], expected_incarnations={"one": expected})
    assert result.previous_pid == original.pid
    assert observed == [None]


def test_direct_claim_racing_final_preflight_is_not_erased_or_signaled(tmp_path, monkeypatch):
    comms, stopped = setup_owners(tmp_path, monkeypatch)
    snapshot = comms.registry.snapshot()
    original = snapshot.threads["one"]
    expected = (original.pid, original.created_at, snapshot.admission_generations["one"])
    real_fence = comms.registry.fence_idle_owner

    def racing_fence(thread, *, expected_admission_generation):
        current = comms.registry.require("one")
        comms.registry.register(replace(current, active_turn=ActiveTurn("raced", current.pid)))
        return real_fence(thread, expected_admission_generation=expected_admission_generation)

    monkeypatch.setattr(comms.registry, "fence_idle_owner", racing_fence)
    with pytest.raises(RelationViolationError, match="Idle owner changed before restart fence"):
        comms.restart_owners(["one"], expected_incarnations={"one": expected})
    assert stopped == []
    assert comms.registry.require("one").active_turn.id == "raced"


def test_explicit_start_cannot_reopen_stopped_live_restart_fence(tmp_path, monkeypatch):
    comms, stopped = setup_owners(tmp_path, monkeypatch)
    original = comms.registry.require("one")
    snapshot = comms.registry.snapshot()
    comms.registry.fence_idle_owner(
        original, expected_admission_generation=snapshot.admission_generations["one"]
    )
    with pytest.raises(RelationViolationError, match="Cannot reactivate a stopped incarnation"):
        comms.start("one")
    assert comms.registry.status("one") is ThreadStatus.STOPPED
    assert comms.registry.require("one").pid == original.pid
    assert stopped == []


def test_start_racing_fence_after_proof_cannot_reopen_admission(tmp_path, monkeypatch):
    comms, stopped = setup_owners(tmp_path, monkeypatch)
    original = comms.registry.require("one")
    snapshot = comms.registry.snapshot()
    expected_admission_generation = snapshot.admission_generations["one"]

    def proof_and_fence(thread, wait=True):
        if wait:
            comms.registry.fence_idle_owner(
                original, expected_admission_generation=expected_admission_generation
            )
        return True

    monkeypatch.setattr(comms, "_is_local_participant", proof_and_fence)
    with pytest.raises(RelationViolationError):
        comms.start("one")
    assert comms.registry.status("one") is ThreadStatus.STOPPED
    assert comms.registry.require("one").active_turn is None
    assert stopped == []


def test_bulk_preflight_refuses_busy_before_stopping_any_owner(tmp_path, monkeypatch):
    comms, stopped = setup_owners(tmp_path, monkeypatch)
    busy = comms.registry.require("two")
    comms.registry.register(replace(busy, active_turn=ActiveTurn("turn", busy.pid)))
    with pytest.raises(ValueError, match="active turn"):
        comms.restart_owners()
    assert stopped == []


def test_restart_rejects_unverifiable_or_nonrunning_owners(tmp_path, monkeypatch):
    comms, stopped = setup_owners(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="no running owner"):
        comms.restart_owners(["stopped"])
    monkeypatch.setattr(comms, "_is_local_participant", lambda thread: False)
    with pytest.raises(ValueError, match="unverifiable"):
        comms.restart_owners(["one"])
    assert not stopped


def test_restart_refuses_self(tmp_path, monkeypatch):
    comms, stopped = setup_owners(tmp_path, monkeypatch)
    current = comms.registry.require("one")
    comms.registry.register(replace(current, pid=os.getpid()))
    with pytest.raises(ValueError, match="itself"):
        comms.restart_owners(["one"])
    assert not stopped


def test_restart_cli_reports_pid_changes(tmp_path, monkeypatch, capsys):
    comms, _ = setup_owners(tmp_path, monkeypatch)
    monkeypatch.setattr("agent_comms.cli.wire", lambda root: comms)
    assert main(["--root", str(tmp_path), "restart", "--name", "one", "--agent-bin", "pi"]) == 0
    assert json.loads(capsys.readouterr().out)["restarted"] == [
        {"thread": "one", "previous_pid": 10001, "pid": 11001}
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner process fixture")
async def test_real_idle_owner_is_replaced_without_losing_session(tmp_path, monkeypatch):
    from agent_comms.runtime import socket_path

    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    session = tmp_path / "session.jsonl"
    session.write_text("")
    comms = wire(tmp_path / "wire")
    comms.register(Thread("worker", frozenset(), str(tmp_path), session_file=str(session)))
    owner = comms.ensure_owner("worker", agent_bin="/bin/echo")

    async def ready(pid):
        async with asyncio.timeout(10):
            while not socket_path(comms.root, pid).exists():
                assert comms._process_alive(pid)
                await asyncio.sleep(0.05)

    try:
        await ready(owner.pid)
        receipts = await asyncio.to_thread(comms.restart_owners, ["worker"], agent_bin="/bin/echo")
        assert receipts[0].previous_pid == owner.pid
        assert receipts[0].pid != owner.pid
        assert not comms._process_alive(owner.pid)
        await ready(receipts[0].pid)
        assert comms.registry.require("worker").session_file == str(session)
        assert comms.registry.require("worker").active_turn is None
    finally:
        await asyncio.to_thread(comms.stop, "worker")
