"""Spawned workers acquire only their operation dependencies, not the pytest fixture and installer stack."""

from __future__ import annotations

from multiprocessing.synchronize import Event
from pathlib import Path
from typing import Any

import agent_comms.storage.store_files as store_files
from agent_comms.comms import wire
from agent_comms.execution.runtime_observations import AgentInfo, ContextUsageObservation
from agent_comms.pi_counts import PiCount
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.activity import ActivityState
from agent_comms.registry.thread_model import DeclaredModel
from agent_comms.registry.threads import NativeThread


def _send_messages(root: str, sender: str, count: int, start: Event) -> None:
    comms = wire(root)
    start.wait()
    for index in range(count):
        comms.messaging.send(sender, "#all", f"{sender}:{index}").message.message_id


def _register_thread(root: str, name: str, start: Event) -> None:
    start.wait()
    comms = wire(root)
    comms.registry.declare(NativeThread(name=name, tags=frozenset({'worker'}), worktree=Checkout(Path('/tmp'))))


def _register_thread_same_tick(root: str, name: str, start: Event) -> None:
    start.wait()

    store_files.time.time = lambda: 1_700_000_000.0
    comms = wire(root)
    comms.registry.declare(NativeThread(name=name, tags=frozenset({'worker'}), worktree=Checkout(Path('/tmp'))))


def _claim_thread(root: str, result_queue: Any, start: Event) -> None:
    start.wait()
    thread = wire(root).threads.claim_thread("project", tags=frozenset({"acp"}), worktree=Checkout(Path("/tmp/project")))
    result_queue.put(thread.name)


def _write_runtime_state(root: str, name: str, start: Event) -> None:
    comms = wire(root)
    start.wait()
    comms.agents.observe_runtime_info(name, ContextUsageObservation(context_used=PiCount.from_wire(25)))
    comms.agents.observe_runtime_info(name, AgentInfo(model=DeclaredModel(f"provider/{name}"), context_size=PiCount.from_wire(100)))
    comms.agents.set_activity(name, ActivityState.WORKING, f"work-{name}")
    comms.ledger.merge({name: "ready"}, author=name)
