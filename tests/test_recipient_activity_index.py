"""Registry snapshots own recipient lookup; activity reads own their requested set."""

import hashlib
from dataclasses import replace
from unittest.mock import patch

import pytest

from agent_comms.activity import Activity, ActivityLog, ActivityState
from agent_comms.agent_activity import UnavailableRecipientActivity
from agent_comms.audience_manifest import FrozenRecipient
from agent_comms.comms import Comms
from agent_comms.errors import RelationViolationError, UnregisteredThreadError
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_presentation import GoalWaitTarget
from agent_comms.notification_assignment import NotificationAssignment
from agent_comms.presentation import AllOutcomes, MessageNotification, OwnerOutcomes
from agent_comms.messages import InfoKind, Message
from agent_comms.read_ledger import ReadDocument, ReadLedger
from agent_comms.read_basis import ConversationCursor, ReadCursor
from agent_comms.registry_provenance import RegisteredRecipient, RetiredRecipient
from agent_comms.registry_provenance import RegistryProvenance
from agent_comms.registry_document import RegistrySnapshot
from agent_comms.thread_identity import (
    DeletedThreadIncarnation,
    RecordedThreadIncarnation,
    ThreadIncarnation,
    UnrecordedThreadIncarnation,
)
from agent_comms.threads import Thread
from goal_owner_fixture import canonical_goal_wire


def test_recipient_index_is_built_once_and_reused_for_small_window(tmp_path):
    comms = canonical_goal_wire(tmp_path / "bus")
    for index in range(12):
        comms.registry.declare(comms.registry.new_thread(f'peer-{index}', frozenset(), str(tmp_path), created_at=1700000000.0 + index))
    sent = comms.messaging.send_user_message("peer-7", "original recipient", worktree=str(tmp_path))
    with comms.bus.log.checked_read() as source:
        original = source.delivery(sent.seq)
    snapshot = comms.registry.snapshot()
    assert snapshot.threads
    wanted = snapshot.threads["peer-7"]
    lookup = wanted.incarnation.lookup
    recipient = FrozenRecipient(lookup, wanted.name)
    assert recipient in original.audience.recipients
    assert snapshot.by_lookup[lookup] is wanted
    assert snapshot.recipient(recipient, original) == RegisteredRecipient(wanted)
    with pytest.raises(UnregisteredThreadError, match="not in the original message"):
        snapshot.recipient(FrozenRecipient("not-a-recipient", "gone"), original)
    with pytest.raises(RelationViolationError, match="recipient name"):
        snapshot.recipient(FrozenRecipient(lookup, "peer-8"), original)
    with patch.object(RecordedThreadIncarnation, "lookup", property(
        lambda self: (_ for _ in ()).throw(AssertionError("rehashing unrelated registry"))
    )):
        observations = comms.agents.observe_recipients(
            ((original, recipient), (original, recipient)), snapshot=snapshot,
        )
    assert set(observations) == {lookup}
    comms.registry.unregister(wanted.name)
    comms.registry.remove(wanted.name)
    retired_snapshot = comms.registry.snapshot()
    assert retired_snapshot.recipient(recipient, original) == RetiredRecipient(lookup)
    retired = comms.agents.observe_recipients(
        ((original, recipient),), snapshot=retired_snapshot,
    )
    assert isinstance(retired[lookup], UnavailableRecipientActivity)
    assert ReadLedger(tmp_path / "reads.json").displayed_recipient(
        original, recipient, retired_snapshot
    ) == ()
    window = MessageNotification.delivery_window(
        comms.root, comms.registry, (original,),
        NotificationAssignment.read(comms.root, (original,)), viewer=AllOutcomes(),
    )
    assert (sent.seq, sent.message_id) in window
    assert all(not item.displayed_to for item in window[(sent.seq, sent.message_id)])


def test_archived_duplicate_birth_keeps_names_without_selecting_an_arbitrary_owner(tmp_path):
    comms = Comms(tmp_path / "bus")
    for name, birth in (("old", 1700.0), ("second", 1701.0)):
        comms.registry.declare(comms.registry.new_thread(name, frozenset(), str(tmp_path), created_at=birth))
    source = comms.registry.snapshot()
    with pytest.raises(RelationViolationError, match="creation identities collide"):
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
    with pytest.raises(ValueError, match="positive recorded birth"):
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
