"""Publication content identity, separate from actual bus delivery evidence."""

from dataclasses import replace

from agent_comms.messaging.mentions import ThreadMention
from agent_comms.messaging.messages import InfoKind, Message, MessageKind
from agent_comms.registry.thread_identity import UserThreadRole


def test_publication_snapshot_is_frozen_content_not_delivery_enrichment():
    original = Message("owner", "#team", "answer @sender", InfoKind, timestamp=42.0)
    delivered = replace(
        original,
        seq=31,
        sender_role=UserThreadRole,
        mentions=ThreadMention.find(original.body, lambda name: name),
    )
    assert delivered.publication_snapshot == original
    assert delivered.publication_snapshot.message_id == original.message_id
    assert delivered.seq == 31 and delivered.sender_role == UserThreadRole
    assert delivered.mentions  # Capture does not mutate or strip the durable source.
    for kind in MessageKind.members_with(MessageKind):
        if kind is not original.type:
            changed = replace(delivered, type=kind)
            assert changed.message_id == original.message_id
            assert changed.publication_snapshot != original
    assert replace(delivered, role=delivered.role.as_notice()).publication_snapshot != original

