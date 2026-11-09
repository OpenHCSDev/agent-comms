"""Unread counts retain identity and role rules without per-row Python callbacks."""

import sqlite3
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path

import pytest

from agent_comms.comms import Comms
from agent_comms.messaging.audience_manifest import freeze_audience
from agent_comms.messaging.bus import Bus, PrivateRecord
from agent_comms.messaging.frozen_recipient import FrozenRecipient
from agent_comms.messaging.messages import InfoKind, Message
from agent_comms.messaging.wake import OrdinaryControl, resolve_wake_cohort
from agent_comms.nominal.field_codec import ThreadLookup
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_identity import (
    AgentThreadRole,
    UserThreadRole,
)
from agent_comms.registry.threads import NativeThread


class NoPythonCallbacks(sqlite3.Connection):
    def create_function(
        self, name: str, narg: int, func: Callable[..., object] | None, *,
        deterministic: bool = False,
    ) -> None:
        raise AssertionError(f"Unread counting registered a Python callback: {name}")


def test_pending_counts_need_no_python_callback(
    comms: Comms, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    comms.channels.create_tag("comms")
    names = tuple(f"actor-{index}" for index in range(500))
    # The declared roles' own rules: a user counts from its boundary, an agent only once
    # delivery was recorded. A test-only role would join ThreadRole in every process that
    # imports this file and change the bus schema that process writes.
    human = names[0]
    roles = (UserThreadRole,) + (AgentThreadRole,) * (len(names) - 1)
    for index, (name, role) in enumerate(zip(names, roles, strict=True)):
        comms.registry.declare(NativeThread(
            name, frozenset({"comms"}), Checkout(tmp_path / name),
            created_at=10000.0 + index,
            role=role))
    comms.registry.declare(NativeThread("old-sender", frozenset({"comms"}), Checkout(tmp_path / "sender"),
                                  created_at=20000.0, role=UserThreadRole))
    with comms.registry.store.editing() as edit:
        edit.document.rename("old-sender", "sender")
        edit.commit()
    snapshot = comms.registry.snapshot()
    sender = snapshot.require("sender").incarnation.require_recorded()
    retired = ThreadLookup("f" * 32)
    assert retired != sender.lookup
    assert snapshot.unread_since(names[1], 30000.0, False) is None
    recipients = {name: snapshot.require(name).incarnation.require_recorded().lookup
                  for name in names}
    expected = dict.fromkeys(names, 0)

    def history() -> Iterator[PrivateRecord]:
        for seq in range(1, 20001):
            recipient = names[(seq - 1) % len(names)]
            current = seq % 10 != 0
            recent = seq % 17 != 0
            message = Message(
                "old-sender" if seq % 2 else "sender", "#comms", f"retained {seq}", InfoKind,
                seq=seq, timestamp=30000.0 if recent else 9999.0,
            )
            if current and recent:
                expected[human] += 1
                if recipient != human:
                    expected[recipient] += 1
            audience = freeze_audience(
                message, (FrozenRecipient(recipients[recipient], recipient),), "c" * 64,
                sender_lookup=sender.lookup if current else retired, sender_name=message.sender,
            )
            yield PrivateRecord.from_message(
                audience, decisions=resolve_wake_cohort(audience, control=OrdinaryControl),
                control=OrdinaryControl.declared_name,
            )

    Bus.install(Bus.path_in(comms.root), history=history())
    assert comms.bus.bus.revision() == 20000
    monkeypatch.setattr(sqlite3, "connect", partial(sqlite3.connect, factory=NoPythonCallbacks))
    assert comms.bus.pending_counts_all((*names, sender.name)) == {**expected, sender.name: 0}
