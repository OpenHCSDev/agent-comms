"""Multiprocess and crash-recovery tests for the shared wire stores."""

from __future__ import annotations

from agent_comms.repo import Checkout
import multiprocessing
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from multiprocessing.context import BaseContext
from multiprocessing.synchronize import Event
from pathlib import Path
from typing import Any


import agent_comms.store_files as store_files
from agent_comms.activity import ActivityState
from agent_comms.agent_events import AgentInfo, ContextUsageObservation
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.message_page import PageBounds
from agent_comms.pi_counts import PiCount
from bus_history import read_history
from conftest import installed_comms
from agent_comms.thread_model import DeclaredModel
from agent_comms.threads import Thread


def _send_messages(root: str, sender: str, count: int, start: Event) -> None:
    comms = wire(root)
    start.wait()
    for index in range(count):
        comms.messaging.send(sender, "#all", f"{sender}:{index}").message.message_id


def _register_thread(root: str, name: str, start: Event) -> None:
    start.wait()
    comms = wire(root)
    comms.registry.declare(Thread(name=name, tags=frozenset({'worker'}), worktree=Checkout(Path('/tmp'))))


def _register_thread_same_tick(root: str, name: str, start: Event) -> None:
    start.wait()

    store_files.time.time = lambda: 1_700_000_000.0
    comms = wire(root)
    comms.registry.declare(Thread(name=name, tags=frozenset({'worker'}), worktree=Checkout(Path('/tmp'))))


def _claim_thread(root: str, result_queue: Any, start: Event) -> None:
    start.wait()
    thread = wire(root).threads.claim_thread("project", tags=frozenset({"acp"}), worktree="/tmp/project")
    result_queue.put(thread.name)


def _write_runtime_state(root: str, name: str, start: Event) -> None:
    comms = wire(root)
    start.wait()
    comms.agents.observe_runtime_info(name, ContextUsageObservation(context_used=PiCount.from_wire(25)))
    comms.agents.observe_runtime_info(name, AgentInfo(model=DeclaredModel(f"provider/{name}"), context_size=PiCount.from_wire(100)))
    comms.agents.set_activity(name, ActivityState.WORKING, f"work-{name}")
    comms.ledger.merge({name: "ready"}, author=name)


def _run_concurrently(
    ctx: BaseContext,
    target: Callable[..., None],
    arguments: Sequence[tuple[object, ...]],
) -> None:
    start = ctx.Event()
    processes = [ctx.Process(target=target, args=(*args, start)) for args in arguments]
    with ExitStack() as running:
        for process in processes:
            process.start()
            running.callback(process.close)
            running.callback(process.join, 5)
            running.callback(process.kill)
        start.set()
        for process in processes:
            process.join(timeout=30)
            assert process.exitcode == 0


class TestConcurrentWire:

    def test_six_spawned_same_tick_owners_register_distinct_incarnations(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "wire"
        names = [f"worker-{index}" for index in range(6)]
        ctx = multiprocessing.get_context("spawn")
        _run_concurrently(ctx, _register_thread_same_tick, [(str(root), name) for name in names])
        stored = wire(root).registry
        births = [stored.require(name).incarnation.require_recorded() for name in names]
        assert len({birth.wire_birth for birth in births}) == len(names)
        assert len({birth.lookup for birth in births}) == len(names)

    def test_concurrent_senders_preserve_every_message_and_sequence(self, tmp_path: Path) -> None:
        root = tmp_path / "wire"
        comms = installed_comms(root)
        senders = [f"worker-{index}" for index in range(6)]
        for sender in senders:
            comms.registry.declare(Thread(name=sender, tags=frozenset(), worktree=Checkout(Path('/tmp'))))

        ctx = multiprocessing.get_context("spawn")
        _run_concurrently(
            ctx,
            _send_messages,
            [(str(root), sender, 40) for sender in senders],
        )

        messages = read_history(wire(root), bounds=PageBounds(limit=len(senders) * 40))
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
        assert set(comms.agents.runtime_info.read().observations) == set(names)
        snapshot = comms.registry.snapshot()
        assert set(comms.agents.all_activity(snapshot.threads.values(), snapshot=snapshot)) == set(names)
        ledger = FieldCodec.encode(comms.ledger.read())
        assert {name: ledger[name] for name in names} == {name: "ready" for name in names}

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

