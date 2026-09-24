"""Multiprocess and crash-recovery tests for the shared wire stores."""

from __future__ import annotations

import multiprocessing
from collections.abc import Callable, Sequence
from multiprocessing.context import BaseContext
from multiprocessing.synchronize import Event
from pathlib import Path
from typing import Any

import pytest

from agent_comms import ActivityState, Thread, UnregisteredThreadError, wire
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.declarations import RelationViolationError


def _send_messages(root: str, sender: str, count: int, start: Event) -> None:
    comms = wire(root)
    start.wait()
    for index in range(count):
        comms.send(sender, "#all", f"{sender}:{index}")


def _register_thread(root: str, name: str, start: Event) -> None:
    start.wait()
    wire(root).register(Thread(name=name, tags=frozenset({"worker"}), worktree="/tmp"))


def _register_thread_same_tick(root: str, name: str, start: Event) -> None:
    start.wait()
    from agent_comms import declarations

    declarations.time.time = lambda: 1_700_000_000.0
    wire(root).register(Thread(name=name, tags=frozenset({"worker"}), worktree="/tmp"))


def _claim_thread(root: str, result_queue: Any, start: Event) -> None:
    start.wait()
    thread = wire(root).claim_thread("project", tags=frozenset({"acp"}), worktree="/tmp/project")
    result_queue.put(thread.name)


def _write_runtime_state(root: str, name: str, start: Event) -> None:
    comms = wire(root)
    start.wait()
    comms.set_agent_info(name, model=f"provider/{name}", context_used=25, context_size=100)
    comms.set_activity(name, ActivityState.WORKING, f"work-{name}")
    comms.ledger_merge({name: "ready"}, author=name)


def _send_while_deleting(root: str, name: str, start: Event) -> None:
    comms = wire(root)
    start.wait()
    for index in range(100):
        try:
            comms.send(name, "#all", f"race:{index}")
        except UnregisteredThreadError:
            return


def _delete_thread(root: str, name: str, start: Event) -> None:
    comms = wire(root)
    start.wait()
    comms.stop(name)
    comms.delete(name)


def _run_concurrently(
    ctx: BaseContext,
    target: Callable[..., None],
    arguments: Sequence[tuple[object, ...]],
) -> None:
    start = ctx.Event()
    processes = [ctx.Process(target=target, args=(*args, start)) for args in arguments]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=30)
        if process.is_alive():
            process.terminate()
            process.join()
        assert process.exitcode == 0


class TestConcurrentWire:
    def test_default_same_tick_owners_get_distinct_durable_claim_identities(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # Windows 3.11 can return the same clock tick to separate spawned
        # workers. Explicit caller-supplied duplicate identities still fail.
        monkeypatch.setattr("agent_comms.declarations.time.time", lambda: 1_700_000_000.0)
        root = tmp_path / "wire"
        first = Thread(name="first", tags=frozenset(), worktree="/tmp")
        second = Thread(name="second", tags=frozenset(), worktree="/tmp")
        assert type(first.created_at) is float
        assert first.created_at == second.created_at
        wire(root).register(first)
        wire(root).register(second)
        stored = wire(root).registry
        first_time = stored.require("first").created_at
        second_time = stored.require("second").created_at
        assert first_time == first.created_at
        assert first_time != second_time
        assert stable_thread_lookup(first_time) != stable_thread_lookup(second_time)
        assert "_generated_created_at" not in stored.require("second").to_wire()
        with pytest.raises(RelationViolationError, match="creation identities collide"):
            wire(root).register(
                Thread(
                    name="explicit",
                    tags=frozenset(),
                    worktree="/tmp",
                    created_at=first.created_at,
                )
            )
        assert set(wire(root).registry.all_threads()) == {"first", "second"}

    def test_six_spawned_same_tick_owners_register_distinct_incarnations(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "wire"
        names = [f"worker-{index}" for index in range(6)]
        ctx = multiprocessing.get_context("spawn")
        _run_concurrently(ctx, _register_thread_same_tick, [(str(root), name) for name in names])
        stored = wire(root).registry
        times = [stored.require(name).created_at for name in names]
        assert len(set(times)) == len(names)
        assert len({stable_thread_lookup(created_at) for created_at in times}) == len(names)

    def test_concurrent_senders_preserve_every_message_and_sequence(self, tmp_path: Path) -> None:
        root = tmp_path / "wire"
        comms = wire(root)
        senders = [f"worker-{index}" for index in range(6)]
        for sender in senders:
            comms.register(Thread(name=sender, tags=frozenset(), worktree="/tmp"))

        ctx = multiprocessing.get_context("spawn")
        _run_concurrently(
            ctx,
            _send_messages,
            [(str(root), sender, 40) for sender in senders],
        )

        messages = list(wire(root).full_history())
        assert len(messages) == len(senders) * 40
        assert [message.seq for message in messages] == list(range(1, len(messages) + 1))
        assert len({message.body for message in messages}) == len(messages)

    def test_concurrent_snapshot_updates_do_not_lose_peers_or_state(self, tmp_path: Path) -> None:
        root = tmp_path / "wire"
        names = [f"worker-{index}" for index in range(6)]
        ctx = multiprocessing.get_context("spawn")
        _run_concurrently(
            ctx,
            _register_thread,
            [(str(root), name) for name in names],
        )
        assert set(wire(root).registry.all_threads()) == set(names)

        _run_concurrently(
            ctx,
            _write_runtime_state,
            [(str(root), name) for name in names],
        )
        comms = wire(root)
        assert set(comms.all_agent_info()) == set(names)
        assert set(comms.all_activity()) == set(names)
        assert {name: comms.ledger_read()[name] for name in names} == {
            name: "ready" for name in names
        }

    def test_concurrent_claims_allocate_unique_names(self, tmp_path: Path) -> None:
        root = tmp_path / "wire"
        ctx = multiprocessing.get_context("spawn")
        start = ctx.Event()
        result_queue = ctx.Queue()
        processes = [
            ctx.Process(target=_claim_thread, args=(str(root), result_queue, start))
            for _ in range(6)
        ]
        for process in processes:
            process.start()
        start.set()
        for process in processes:
            process.join(timeout=30)
            if process.is_alive():
                process.terminate()
                process.join()
            assert process.exitcode == 0

        names = {result_queue.get(timeout=5) for _ in processes}
        assert names == {"project", *(f"project-{index}" for index in range(2, 7))}

    def test_delete_cannot_leave_late_messages(self, tmp_path: Path) -> None:
        root = tmp_path / "wire"
        comms = wire(root)
        comms.register(Thread(name="delete-me", tags=frozenset(), worktree="/tmp"))
        comms.register(Thread(name="keeper", tags=frozenset(), worktree="/tmp"))

        ctx = multiprocessing.get_context("spawn")
        start = ctx.Event()
        sender = ctx.Process(
            target=_send_while_deleting,
            args=(str(root), "delete-me", start),
        )
        deleter = ctx.Process(
            target=_delete_thread,
            args=(str(root), "delete-me", start),
        )
        sender.start()
        deleter.start()
        start.set()
        for process in (sender, deleter):
            process.join(timeout=30)
            if process.is_alive():
                process.terminate()
                process.join()
            assert process.exitcode == 0

        result = wire(root)
        assert "delete-me" not in result.registry
        assert all(message.sender != "delete-me" for message in result.full_history())


class TestCrashRecovery:
    def test_message_bus_ignores_then_quarantines_truncated_tail(self, wired: Any) -> None:
        wired.send("PR111", "#all", "complete")
        with open(wired.bus._path, "ab") as output:
            output.write(b'{"seq": 2, "from": "broken"')

        assert [message.body for message in wired.full_history()] == ["complete"]
        wired.send("PR111", "#all", "after recovery")

        messages = list(wired.full_history())
        assert [message.body for message in messages] == ["complete", "after recovery"]
        assert [message.seq for message in messages] == [1, 2]
        assert (wired.bus._path.parent / "bus.jsonl.corrupt").exists()

    def test_activity_log_recovers_from_truncated_tail(self, wired: Any) -> None:
        wired.set_activity("PR111", ActivityState.THINKING, "first")
        with open(wired.activity._path, "ab") as output:
            output.write(b'{"thread": "PR111"')

        assert wired.activity_of("PR111").detail == "first"
        wired.set_activity("PR111", ActivityState.WORKING, "second")
        assert wired.activity_of("PR111").detail == "second"
        assert (wired.activity._path.parent / "activity.jsonl.corrupt").exists()
