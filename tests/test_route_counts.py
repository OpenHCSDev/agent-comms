"""Unread counts retain identity and role rules without per-row Python callbacks."""

import sqlite3
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path

import pytest

from agent_comms.audience_manifest import FrozenRecipient, freeze_audience
from agent_comms.bus import Bus, PrivateRecord
from agent_comms.comms import Comms
from agent_comms.field_codec import ThreadLookup
from agent_comms.messages import InfoKind, Message
from agent_comms.thread_identity import AgentThreadRole, UserThreadRole
from agent_comms.threads import Thread
from agent_comms.wake import OrdinaryControl, resolve_wake_cohort


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
    human = names[0]
    for index, name in enumerate(names):
        comms.registry.declare(Thread(
            name, frozenset({"comms"}), str(tmp_path / name),
            created_at=10000.0 + index,
            role=UserThreadRole if name == human else AgentThreadRole,
        ))
    comms.registry.declare(Thread("old-sender", frozenset(), str(tmp_path / "sender"),
                                  created_at=20000.0))
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
    assert comms.bus.pending_counts_all(names) == expected
