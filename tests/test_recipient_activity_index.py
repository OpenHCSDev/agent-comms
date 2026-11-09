"""Registry snapshots own recipient lookup and viewer selection."""


from dataclasses import replace

import pytest

from agent_comms.comms import wire
from agent_comms.errors import RelationViolationError
from agent_comms.messaging.frozen_recipient import FrozenRecipient
from agent_comms.messaging.message_notification import MessageNotification
from agent_comms.messaging.messages import InfoKind, Message
from agent_comms.messaging.notification_evidence import AllOutcomes, OwnerOutcomes
from agent_comms.messaging.read_basis import ConversationCursor, ReadCursor
from agent_comms.messaging.read_ledger import (
    ReadDocument,
    ReadLedger,
)
from agent_comms.notification_assignment import NotificationAssignment, NotificationSelection
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.registry_document import RegistrySnapshot, ThreadRows
from agent_comms.registry.registry_provenance import RegistryProvenance
from agent_comms.registry.thread_identity import UnrecordedThreadIncarnation
from agent_comms.registry.threads import NativeThread
from goal_owner_fixture import canonical_goal_wire


def test_archived_duplicate_birth_keeps_names_without_selecting_an_arbitrary_owner(tmp_path):
    comms = wire(tmp_path / "bus")
    for name, birth in (("old", 1700.0), ("second", 1701.0)):
        comms.registry.declare(NativeThread(name, frozenset(), Checkout(tmp_path), created_at=birth))
    source = comms.registry.snapshot()
    with pytest.raises(RelationViolationError):
        RegistrySnapshot.capture(replace(comms.registry.store.read(), threads=ThreadRows.from_threads({
            **source.threads,
            "second": replace(source.threads["second"], created_at=1700.0),
        }))).lookup_names
    recorded = RegistryProvenance.capture(source)
    archived = replace(recorded, threads={
        **recorded.threads,
        "second": replace(recorded.threads["second"], created_at=1700.0),
    })
    lookup = source.threads["old"].incarnation.lookup
    ledger = ReadLedger(tmp_path / "archive-reads.json")
    message = Message('old', '#archive', 'seen', InfoKind, seq=7, timestamp=1700.0)
    conversation = ledger.conversation(message, archived)
    document = ReadDocument(messages={
        thread.incarnation: ReadCursor({conversation: ConversationCursor(exceptions=(7,))})
        for thread in archived.threads.values()
    })
    for name in ("old", "second"):
        assert ledger.historical_displayed_recipient(
            message, FrozenRecipient(lookup, name), archived, document=document
        ) == (archived.threads[name].incarnation,)
    assert ledger.historical_displayed_recipient(
        message, FrozenRecipient("wrong-lookup", "old"), archived, document=document
    ) == ()
    with pytest.raises(ValueError):
        replace(source.threads["second"], created_at=0.0)
    zero = replace(recorded.threads["second"], created_at=0.0)
    unrecorded = replace(recorded, threads={**recorded.threads, "second": zero})
    assert isinstance(unrecorded.threads["second"].incarnation, UnrecordedThreadIncarnation)


@pytest.mark.parametrize(("viewer_of", "seen"), [
    (lambda lookups: AllOutcomes(), {"peer-a", "peer-b"}),
    (lambda lookups: OwnerOutcomes(lookups["sender"]), {"peer-a", "peer-b"}),
    (lambda lookups: OwnerOutcomes(lookups["peer-a"]), {"peer-a"}),
])
def test_viewer_selects_the_outcomes_of_a_delivery_window(tmp_path, viewer_of, seen):
    comms = canonical_goal_wire(tmp_path / "bus")
    for name in ("sender", "peer-a", "peer-b"):
        comms.registry.declare(NativeThread(name, frozenset({"ci"}), Checkout(tmp_path)))
    sent = comms.messaging.send("sender", "#ci", "channel outcome").message
    original = comms.bus.bus.delivery(sent.seq)
    lookups = {name: thread.incarnation.lookup
               for name, thread in comms.registry.snapshot().threads.items()}
    viewer = viewer_of(lookups)
    window = MessageNotification.delivery_window(
        comms.root, comms.registry,
        NotificationAssignment.read(comms.root, NotificationSelection.of(viewer, (original,))),
    )
    assert {item.recipient for item in window.outcomes[(sent.seq, sent.message_id)]} == seen


def test_a_viewer_follows_frozen_lookups_through_rename_and_never_into_a_rebirth(tmp_path):
    comms = canonical_goal_wire(tmp_path / "bus")
    for name in ("sender", "peer-a", "peer-b"):
        comms.registry.declare(NativeThread(name, frozenset({"ci"}), Checkout(tmp_path)))
    sent = comms.messaging.send("sender", "#ci", "channel outcome").message
    original = comms.bus.bus.delivery(sent.seq)
    key = (sent.seq, sent.message_id)

    def seen_by(name: str) -> set[str]:
        viewer = OwnerOutcomes.of(comms.registry.require(name).incarnation)
        read = NotificationAssignment.read(comms.root, NotificationSelection.of(viewer, (original,)))
        assert read.selection.viewer == viewer
        window = MessageNotification.delivery_window(comms.root, comms.registry, read)
        return {item.recipient for item in window.outcomes[key]}

    assert seen_by("peer-a") == {"peer-a"}
    comms.registry.rename("peer-a", "renamed-a")
    assert seen_by("renamed-a") == {"peer-a"}
    comms.registry.remove("peer-b")
    comms.registry.declare(NativeThread("peer-b", frozenset({"ci"}), Checkout(tmp_path)))
    assert seen_by("peer-b") == set()
    assert seen_by("sender") == {"peer-a", "peer-b"}


def test_a_public_root_without_a_coordinator_keeps_its_viewer_and_projects_visible_recipients(tmp_path):
    from agent_comms.coordination.database import CoordinationStore

    comms = wire(tmp_path / "bus")
    for name in ("sender", "peer-a", "peer-b"):
        comms.registry.declare(NativeThread(name, frozenset({"ci"}), Checkout(tmp_path)))
    sent = comms.messaging.send("sender", "#ci", "channel outcome").message
    original = comms.bus.bus.delivery(sent.seq)
    CoordinationStore.path_in(comms.root).unlink(missing_ok=True)
    viewer = OwnerOutcomes.of(comms.registry.require("peer-a").incarnation)
    read = NotificationAssignment.read(comms.root, NotificationSelection.of(viewer, (original,)))
    assert (read.sources, read.rows, read.consumption, read.selection.viewer) == ((original,), (), {}, viewer)
    window = MessageNotification.delivery_window(comms.root, comms.registry, read)
    assert {item.recipient for item in window.outcomes[(sent.seq, sent.message_id)]} == {"peer-a"}


def test_a_private_root_without_a_coordinator_still_refuses_notification_reads(private_comms):
    from agent_comms.coordination.database import CoordinationStore
    from agent_comms.coordination.errors import PublicationActivationBlocked

    for name in ("sender", "peer-a"):
        private_comms.registry.declare(NativeThread(name, frozenset({"ci"}), Checkout(private_comms.root)))
    sent = private_comms.messaging.send("sender", "#ci", "channel outcome").message
    original = private_comms.bus.bus.delivery(sent.seq)
    CoordinationStore.path_in(private_comms.root).unlink()
    with pytest.raises(PublicationActivationBlocked):
        NotificationAssignment.read(private_comms.root, NotificationSelection.of(AllOutcomes(), (original,)))
