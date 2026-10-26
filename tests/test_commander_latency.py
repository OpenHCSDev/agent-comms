"""Commander replies select metadata, not message bodies or every registry row."""

import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.audience_manifest import FrozenRecipient, freeze_audience
from agent_comms.bus import Bus, PrivateRecord
from agent_comms.bus_tables import MessageIdentitySource
from agent_comms.channel_targets import CommandTarget
from agent_comms.commander.order import Commander
from agent_comms.commander_service import CommanderService
from agent_comms.comms import Comms, wire
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import MessageId
from agent_comms.installer.backup import StoreBackup
from agent_comms.message_reference import MessageReference
from agent_comms.messages import InfoKind, Message
from agent_comms.registry_document import RegistrySnapshot
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.thread_model import DeclaredModel
from agent_comms.threads import Thread
from agent_comms.wake import OrdinaryControl, resolve_wake_cohort


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


def test_commander_verb_answers_under_five_seconds(
    comms: Comms, tmp_path: Path, configured_repository: Callable[[Comms], Commander],
) -> None:
    comms.channels.create_tag("comms")
    names = tuple(f"worker-{index}" for index in range(500))
    for index, name in enumerate((*names, "lead", CommanderService.sender)):
        comms.registry.declare(Thread(
            name, frozenset({"comms"}), str(tmp_path / name),
            created_at=10000.0 + index, execution=ExternalThreadExecution,
            model=DeclaredModel("anthropic/claude-opus-4-6"),
        ))
    configured_repository(comms)
    snapshot = comms.registry.snapshot()
    rows = (
        (Message("lead", names[(seq - 1) % len(names)], f"retained {seq}", InfoKind,
                 seq=seq, timestamp=30000.0), names[(seq - 1) % len(names)])
        for seq in range(1, 20001)
    )
    Bus.install(Bus.path_in(comms.root), history=recorded_history(rows, snapshot))
    copied_root = tmp_path / "copied-bus"
    StoreBackup.copy_tree(comms.root, copied_root)
    copied = wire(copied_root)
    assert len(copied.registry.snapshot().threads) == 502
    assert copied.bus.bus.revision() == 20000
    body = tmp_path / "reply.txt"
    body.write_text("The measurement is recorded.\n")
    checkout = Path(__file__).resolve().parents[1]
    started = time.monotonic()
    result = subprocess.run(
        (sys.executable, "-c", "from agent_comms.commander.cli import main; main()",
         "--root", str(copied_root), "order", "say", names[0], str(body)),
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
    for index, name in enumerate(("lead", "sender", "other")):
        comms.registry.declare(Thread(name, frozenset({"comms"}), str(tmp_path / name),
                                      created_at=10000.0 + index))
    originals = (
        Message("sender", "#comms", "channel", InfoKind, seq=1, timestamp=30000.0),
        Message("sender", "lead", "direct", InfoKind, seq=2, timestamp=30000.0),
        Message("other", "lead", "unrelated", InfoKind, seq=3, timestamp=30000.0),
    )
    Bus.install(Bus.path_in(comms.root), history=recorded_history(
        ((message, "lead") for message in originals), comms.registry.snapshot(),
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
