"""Team replies select metadata, not message bodies or every registry row."""

import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.comms import Comms, wire
from agent_comms.errors import RelationViolationError
from agent_comms.installer.backup import StoreBackup
from agent_comms.messaging.audience_manifest import freeze_audience
from agent_comms.messaging.bus import Bus, PrivateRecord
from agent_comms.messaging.bus_tables import MessageIdentitySource
from agent_comms.messaging.channel_targets import CommandTarget
from agent_comms.messaging.frozen_recipient import FrozenRecipient
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.messages import InfoKind, Message
from agent_comms.messaging.wake import OrdinaryControl, resolve_wake_cohort
from agent_comms.nominal.field_codec import MessageId
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.registry.thread_model import DeclaredModel
from agent_comms.registry.threads import ExternalThread, NativeThread
from agent_comms.team.order import Team


def recorded_history(
    rows: Iterable[tuple[Message, str]], snapshot: RegistrySnapshot,
) -> Iterator[PrivateRecord]:
    for message, recipient in rows:
        sender = snapshot.require(message.sender).incarnation.require_recorded()
        receiver = snapshot.require(recipient).incarnation.require_recorded()
        audience = freeze_audience(
            message, (FrozenRecipient(receiver.lookup, recipient),), "c" * 64,
            sender_lookup=sender.lookup, sender_name=message.sender,
        )
        yield PrivateRecord.from_message(
            audience, decisions=resolve_wake_cohort(audience, control=OrdinaryControl),
            control=OrdinaryControl.declared_name,
        )


def test_team_verb_answers_under_five_seconds(
    comms: Comms, tmp_path: Path, configured_repository: Callable[[Comms], Team],
) -> None:
    comms.channels.create_tag("comms")
    names = tuple(f"worker-{index}" for index in range(500))
    for index, name in enumerate((*names, "colleague")):
        comms.registry.declare(ExternalThread(
            name, frozenset({"comms"}), Checkout(tmp_path / name),
            created_at=10000.0 + index,
            model=DeclaredModel("anthropic/claude-opus-4-6")))
    configured_repository(comms)
    snapshot = comms.registry.snapshot()
    rows = (
        (Message("colleague", names[(seq - 1) % len(names)], f"retained {seq}", InfoKind,
                 seq=seq, timestamp=30000.0), names[(seq - 1) % len(names)])
        for seq in range(1, 20001)
    )
    Bus.install(Bus.path_in(comms.root), history=recorded_history(rows, snapshot))
    copied_root = tmp_path / "copied-bus"
    StoreBackup.copy_tree(comms.root, copied_root)
    copied = wire(copied_root)
    assert set(copied.registry.snapshot().threads) == {*names, "colleague"}
    assert copied.bus.bus.revision() == 20000
    body = tmp_path / "reply.txt"
    body.write_text("The measurement is recorded.\n")
    checkout = Path(__file__).resolve().parents[1]
    started = time.monotonic()
    result = subprocess.run(
        (sys.executable, "-c", "from agent_comms.team.cli import main; main()",
         "--root", str(copied_root), "order", "say", names[0], str(body), "--as", "colleague"),
        cwd=checkout, env={**os.environ, "PYTHONPATH": str(checkout / "src")},
        capture_output=True, text=True, timeout=30,
    )
    elapsed = time.monotonic() - started
    assert result.returncode == 0, result.stdout + result.stderr
    assert elapsed < 5, f"order say took {elapsed:.3f}s: {result.stdout} {result.stderr}"
    assert copied.bus.bus.revision() > 20000
    assert comms.bus.bus.revision() == 20000
    print(f"order say: {elapsed:.6f}s; recipients=500; retained messages=20000")


@pytest.fixture
def reply_history(comms: Comms, tmp_path: Path) -> tuple[Comms, tuple[MessageReference, ...]]:
    comms.channels.create_tag("comms")
    for index, name in enumerate(("colleague", "sender", "other")):
        comms.registry.declare(NativeThread(name, frozenset({"comms"}), Checkout(tmp_path / name),
                                      created_at=10000.0 + index))
    originals = (
        Message("sender", "#comms", "channel", InfoKind, seq=1, timestamp=30000.0),
        Message("sender", "colleague", "direct", InfoKind, seq=2, timestamp=30000.0),
        Message("other", "colleague", "unrelated", InfoKind, seq=3, timestamp=30000.0),
    )
    Bus.install(Bus.path_in(comms.root), history=recorded_history(
        ((message, "colleague") for message in originals), comms.registry.snapshot(),
    ))
    return comms, tuple(message.reference for message in originals)


@pytest.mark.parametrize(("destination", "expected"), (("#comms", (1,)), ("sender", (2,))))
def test_answering_selects_exact_destination(
    reply_history: tuple[Comms, tuple[MessageReference, ...]],
    destination: str, expected: tuple[int, ...],
) -> None:
    comms, references = reply_history
    with comms.bus.bus.observing() as db:
        selected = MessageIdentitySource.answering(
            db, references, CommandTarget.named(destination), comms.registry.snapshot(),
        )
    assert tuple(reference.seq for reference in selected) == expected


def test_answering_rejects_forged_identity(
    reply_history: tuple[Comms, tuple[MessageReference, ...]],
) -> None:
    comms, references = reply_history
    forged = replace(references[0], message_id=MessageId("f" * 12))
    with comms.bus.bus.observing() as db, pytest.raises(RelationViolationError):
        MessageIdentitySource.answering(
            db, (forged,), CommandTarget.named("#comms"), comms.registry.snapshot(),
        )
