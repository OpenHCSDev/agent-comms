"""Registry snapshots own recipient lookup; activity reads own their requested set."""


from agent_comms.repo import Checkout
from dataclasses import replace

import pytest

from agent_comms.registry_document import ThreadRows

from agent_comms.activity import Activity, ActivityState, ActivityStore
from agent_comms.audience_manifest import FrozenRecipient
from agent_comms.comms import wire
from agent_comms.errors import RelationViolationError, UnregisteredThreadError
from agent_comms.field_codec import ThreadLookup
from agent_comms.notification_assignment import NotificationAssignment
from agent_comms.presentation import AllOutcomes, MessageNotification, OwnerOutcomes
from agent_comms.messages import InfoKind, Message
from agent_comms.read_ledger import ReadDocument, ReadLedger
from agent_comms.read_basis import ConversationCursor, ReadCursor
from agent_comms.registry_provenance import (
    RegisteredRecipient, RegistryProvenance, RetiredRecipient,
)
from agent_comms.registry_document import RegistrySnapshot
from agent_comms.thread_identity import (
    UnrecordedThreadIncarnation,
)
from goal_owner_fixture import canonical_goal_wire
from agent_comms.threads import Thread


def test_archived_duplicate_birth_keeps_names_without_selecting_an_arbitrary_owner(tmp_path):
    comms = wire(tmp_path / "bus")
    for name, birth in (("old", 1700.0), ("second", 1701.0)):
        comms.registry.declare(Thread(name, frozenset(), Checkout(tmp_path), created_at=birth))
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
    assert type(archived) is RegistryProvenance and not hasattr(archived, "lookup_names")
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
    assert not hasattr(unrecorded.threads["second"].incarnation, "lookup")


def test_captured_lookup_keeps_original_recipient_identity_and_retirement(tmp_path):
    comms = canonical_goal_wire(tmp_path / "bus")
    for name in ("sender", "peer", "other"):
        comms.registry.declare(Thread(name, frozenset(), Checkout(tmp_path)))
    sent = comms.messaging.send("sender", "peer", "retained recipient").message
    original = comms.bus.bus.delivery(sent.seq)
    captured = comms.registry.snapshot()
    peer = captured.require("peer")
    recipient = FrozenRecipient(peer.incarnation.lookup, peer.name)
    assert recipient in original.recipients
    assert captured.recipient(recipient, original) == RegisteredRecipient(peer)
    with pytest.raises(RelationViolationError):
        captured.recipient(FrozenRecipient(recipient.recipient_lookup, "other"), original)
    foreign = FrozenRecipient(ThreadLookup.mint(), peer.name)
    with pytest.raises(UnregisteredThreadError):
        captured.recipient(foreign, original)

    comms.registry.rename(peer.name, "renamed")
    renamed = comms.registry.snapshot()
    assert renamed.recipient(recipient, original) == RegisteredRecipient(renamed.require("renamed"))
    comms.registry.unregister("renamed")
    comms.threads.delete("renamed")
    deleted = comms.registry.snapshot()
    assert deleted.recipient(recipient, original) == RetiredRecipient(recipient.recipient_lookup)
    with pytest.raises(UnregisteredThreadError):
        deleted.recipient(foreign, original)
    recreated = comms.registry.declare(Thread("renamed", frozenset(), Checkout(tmp_path)))
    current = comms.registry.snapshot()
    assert recreated.incarnation.lookup != recipient.recipient_lookup
    assert current.recipient(recipient, original) == RetiredRecipient(recipient.recipient_lookup)
    assert captured.recipient(recipient, original) == RegisteredRecipient(peer)


def test_activity_store_selects_only_requested_threads(tmp_path):
    store = ActivityStore(tmp_path)
    for index in range(10):
        store.emit(Activity(f"peer-{index}", ActivityState.THINKING, "working"))
    assert set(store.current_for(("peer-7", "absent"))) == {"peer-7", "absent"}
    assert store.current_for(()) == {}


@pytest.mark.parametrize(("viewer_of", "seen"), [
    (lambda lookups: AllOutcomes(), {"peer-a", "peer-b"}),
    (lambda lookups: OwnerOutcomes(lookups["sender"]), {"peer-a", "peer-b"}),
    (lambda lookups: OwnerOutcomes(lookups["peer-a"]), {"peer-a"}),
])
def test_viewer_selects_the_outcomes_of_a_delivery_window(tmp_path, viewer_of, seen):
    comms = canonical_goal_wire(tmp_path / "bus")
    for name in ("sender", "peer-a", "peer-b"):
        comms.registry.declare(Thread(name, frozenset({"ci"}), Checkout(tmp_path)))
    sent = comms.messaging.send("sender", "#ci", "channel outcome").message
    original = comms.bus.bus.delivery(sent.seq)
    lookups = {name: thread.incarnation.lookup
               for name, thread in comms.registry.snapshot().threads.items()}
    window = MessageNotification.delivery_window(
        comms.root, comms.registry, (original,),
        NotificationAssignment.read(comms.root, (original,)), viewer=viewer_of(lookups),
    )
    assert {item.recipient for item in window.outcomes[(sent.seq, sent.message_id)]} == seen
