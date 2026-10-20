"""Registry snapshots own recipient lookup; activity reads own their requested set."""

from dataclasses import replace

import pytest

from agent_comms.activity import Activity, ActivityLog, ActivityState
from agent_comms.audience_manifest import FrozenRecipient
from agent_comms.comms import Comms
from agent_comms.errors import RelationViolationError
from agent_comms.notification_assignment import NotificationAssignment
from agent_comms.presentation import AllOutcomes, MessageNotification, OwnerOutcomes
from agent_comms.messages import InfoKind, Message
from agent_comms.read_ledger import ReadDocument, ReadLedger
from agent_comms.read_basis import ConversationCursor, ReadCursor
from agent_comms.registry_provenance import RegistryProvenance
from agent_comms.registry_document import RegistrySnapshot
from agent_comms.thread_identity import (
    UnrecordedThreadIncarnation,
)
from goal_owner_fixture import canonical_goal_wire


def test_archived_duplicate_birth_keeps_names_without_selecting_an_arbitrary_owner(tmp_path):
    comms = Comms(tmp_path / "bus")
    for name, birth in (("old", 1700.0), ("second", 1701.0)):
        comms.registry.declare(comms.registry.new_thread(name, frozenset(), str(tmp_path), created_at=birth))
    source = comms.registry.snapshot()
    with pytest.raises(RelationViolationError):
        RegistrySnapshot.capture(replace(comms.registry.store.read(), threads={
            **source.threads,
            "second": replace(source.threads["second"], created_at=1700.0),
        }))
    recorded = RegistryProvenance.capture(source)
    archived = replace(recorded, threads={
        **recorded.threads,
        "second": replace(recorded.threads["second"], created_at=1700.0),
    })
    lookup = source.threads["old"].incarnation.lookup
    assert type(archived) is RegistryProvenance and not hasattr(archived, "by_lookup")
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


def test_activity_log_projects_only_requested_threads(tmp_path):
    log = ActivityLog(tmp_path / "activity.jsonl")
    for index in range(10):
        log.emit(Activity(f"peer-{index}", ActivityState.THINKING, "working"))
    assert set(log.current_for(("peer-7",))) == {"peer-7"}
    assert log.current_for(()) == {}
    assert len(log.all_current()) == 10


@pytest.mark.parametrize(("viewer_of", "seen"), [
    (lambda lookups: AllOutcomes(), {"peer-a", "peer-b"}),
    (lambda lookups: OwnerOutcomes(lookups["sender"]), {"peer-a", "peer-b"}),
    (lambda lookups: OwnerOutcomes(lookups["peer-a"]), {"peer-a"}),
])
def test_viewer_selects_the_outcomes_of_a_delivery_window(tmp_path, viewer_of, seen):
    comms = canonical_goal_wire(tmp_path / "bus")
    for name in ("sender", "peer-a", "peer-b"):
        comms.registry.declare(comms.registry.new_thread(name, frozenset({"ci"}), str(tmp_path)))
    sent = comms.messaging.send("sender", "#ci", "channel outcome").message
    with comms.bus.log.checked_read() as source:
        original = source.delivery(sent.seq)
    lookups = {name: thread.incarnation.lookup
               for name, thread in comms.registry.snapshot().threads.items()}
    window = MessageNotification.delivery_window(
        comms.root, comms.registry, (original,),
        NotificationAssignment.read(comms.root, (original,)), viewer=viewer_of(lookups),
    )
    assert {item.recipient for item in window[(sent.seq, sent.message_id)]} == seen
