"""Counting a listing uses its captured identities, not a borrowed registry lock."""

import multiprocessing
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import ClassVar

import pytest
from multiprocessing.synchronize import Event

from agent_comms.bus_route_counts import BusRouteCounts
from agent_comms.cli_verbs.threads import ThreadsCliCommand
from agent_comms.comms import Comms
from agent_comms.presentation import CountedInbox
from agent_comms.registry_store import RegistryStore
from agent_comms.threads import Thread
from agent_comms.thread_identity import AgentThreadRole, UserThreadRole


def rename_during_count(root: Path, start: Event, done: Event) -> None:
    if not start.wait(15):
        raise RuntimeError("Listing never reached its unread query")
    with RegistryStore.at(root).editing() as edit:
        edit.document.rename("recipient", "renamed")
        edit.commit()
    done.set()


@dataclass
class CountingProbe:
    start: Event
    done: Event
    acquired: list[bool] = field(default_factory=list)

    def progress(self, connection: sqlite3.Connection) -> int:
        connection.set_progress_handler(None, 0)
        self.start.set()
        self.acquired.append(self.done.wait(10))
        return int(not self.acquired[-1])


class CountingConnection(sqlite3.Connection):
    probe: ClassVar[CountingProbe]

    def execute(
        self, sql: str, parameters: Sequence[object] | Mapping[str, object] = (), /,
    ) -> sqlite3.Cursor:
        if sql.lstrip().startswith("WITH") and BusRouteCounts.declared_name in sql:
            self.set_progress_handler(partial(self.probe.progress, self), 100)
        return super().execute(sql, parameters)


def test_thread_listing_counts_outside_the_registry_lock(
    comms: Comms, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    comms.channels.create_tag("team")
    for name in ("sender", "recipient"):
        comms.registry.declare(Thread(name, frozenset({"team"}), str(tmp_path)))
    comms.messaging.send("sender", "recipient", "Count this captured delivery")
    context = multiprocessing.get_context("spawn")
    start, done = context.Event(), context.Event()
    CountingConnection.probe = CountingProbe(start, done)
    writer = context.Process(target=rename_during_count, args=(comms.root, start, done))
    writer.start()
    monkeypatch.setattr(sqlite3, "connect", partial(sqlite3.connect, factory=CountingConnection))
    try:
        listing = ThreadsCliCommand().apply(comms)
        assert CountingConnection.probe.acquired == [True]
        recipient, = (row for row in listing.threads if row.thread.name == "recipient")
        assert recipient.pending == CountedInbox(1)
        assert comms.registry.require("renamed").name == "renamed"
        assert comms.bus.pending_count("renamed") == 1
        assert CountingConnection.probe.acquired == [True, True]
        writer.join(5)
        assert writer.exitcode == 0
    finally:
        writer.terminate()
        writer.join(5)


def test_thread_listing_counts_frozen_deliveries_and_human_history(
    comms: Comms, tmp_path: Path,
) -> None:
    comms.channels.create_tag("team")
    comms.registry.declare(Thread(
        "old-sender", frozenset({"team"}), str(tmp_path), created_at=9000.0,
    ))
    comms.messaging.send("old-sender", "#team", "History before either viewer joined")
    for birth, name, role in ((10000.0, "human", UserThreadRole),
                              (10001.0, "agent", AgentThreadRole)):
        comms.registry.declare(Thread(
            name, frozenset({"team"}), str(tmp_path), created_at=birth, role=role,
        ))
    with comms.registry.store.editing() as edit:
        edit.document.rename("old-sender", "sender")
        edit.commit()
    comms.messaging.send("sender", "#team", "Frozen delivery to both viewers")
    assert comms.bus.pending_counts_all(("human", "agent", "sender")) == {
        "human": 2, "agent": 1, "sender": 0,
    }
