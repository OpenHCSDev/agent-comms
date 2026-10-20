"""Publication content identity, separate from actual bus delivery evidence."""

import json
from typing import Annotated
from dataclasses import dataclass, replace

import pytest

from agent_comms import field_codec
from agent_comms.coordination_response import prepare_fenced_response, publish_fenced_response
from agent_comms.errors import RelationViolationError
from agent_comms.mentions import ThreadMention
from agent_comms.messages import InfoKind, Message, MessageKind, NoticeRole, QuestionKind
from agent_comms.private_bus_checkpoint import install_private_bus_checkpoint
from agent_comms.response_conversation import ResponseConversation
from agent_comms.thread_identity import UserThreadRole
from test_coordination_response import _ready


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


def test_new_declared_delivery_field_inherits_snapshot_without_consumer_edit():
    @dataclass(frozen=True, slots=True)
    class DeliveredMessage(Message):
        delivery_observation: Annotated[int, field_codec.PublicationExclude()] = 0
        annotation: str = "original"

    original = DeliveredMessage("owner", "#team", "answer", InfoKind, timestamp=42.0)
    observed = replace(original, seq=31, delivery_observation=9)
    assert observed.publication_snapshot == original
    assert replace(observed, annotation="changed").publication_snapshot != original


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"type": QuestionKind}, id="type-only"),
        pytest.param({"role": NoticeRole()}, id="notice-only"),
    ],
)
def test_valid_durable_receipt_cannot_substitute_different_publication_content(tmp_path, changes):
    """A fully self-consistent acknowledgement still must match its frozen SQL intent.

    Type and notice are deliberately outside the old hash-derived message ID.
    No forged prefix record or partial envelope is used to reach the owned check.
    """
    case = _ready(tmp_path, direct=True)
    try:
        intent = prepare_fenced_response(
            case.store, case.bus, case.fence, "answer @sender", exact_target=case.reply_target, owner_witness=case.witness
        ).value
        publish_fenced_response(case.store, case.bus, case.fence, exact_target=case.reply_target, owner_witness=case.witness)
        response = case.bus.log.read_keyed_response(intent)
        assert intent.matches_publication(response)
        with case.bus.log.locked():
            snapshot = case.store.snapshots.get("exec")
            conversation = ResponseConversation.capture(
                case.bus, snapshot, snapshot.require_wire_response(case.reply_target)
            )
            changed = replace(response, **changes)
            assert changed.message_id == response.message_id
            record = conversation.record(case.root_id, changed, intent)
            rows = case.bus.log.path.read_bytes().splitlines(keepends=True)
            case.bus.log.path.write_bytes(b"".join(rows[:-1]) + json.dumps(record).encode() + b"\n")
            marker = case.bus.log.private_marker_unlocked()
            verified = list(case.bus.log.verified_records_unlocked(marker))
            assert verified[-1].message == changed  # Full strict durable acknowledgement validation succeeds.
            with pytest.raises(
                RelationViolationError
            ):
                case.bus.log.keyed_publication_unlocked(intent)
            # Public reads borrow this checked lock; changed bytes still fail.
            with pytest.raises(RelationViolationError):
                case.bus.log.read_keyed_response(intent)
            # Certify the isolated changed source while its original writer
            # remains held, never by reopening a missing live checkpoint.
            (case.comms.root / "private_bus_checkpoint.sqlite3").unlink()
            marker.checkpoint_version = None
            marker.checkpoint_seal = None
            case.bus.log.write_metadata_unlocked(marker)
            install_private_bus_checkpoint(case.bus.log, _bus_locked=True)
        before = case.bus.log.path.read_bytes()
        with pytest.raises(RelationViolationError):
            case.bus.log.read_keyed_response(intent)
        assert case.bus.log.path.read_bytes() == before  # No repair, append or replay.
    finally:
        case.close()
