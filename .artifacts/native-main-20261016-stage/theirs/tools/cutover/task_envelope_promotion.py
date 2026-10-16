"""One-shot task member promotion after the original writer certified its preimage.

This is never imported by runtime readers. The old writer owns decoding and
original prefix custody; the target owns the promoted Message and its re-attested
delivery. This module grants no write, owner start, input or wake authority.
"""
from dataclasses import replace

from agent_comms.audience_manifest import freeze_audience
from agent_comms.bus_publication import (
    CommittedDelivery, PRIVATE_WIRE_FIELD, has_private_wire_fields,
    public_envelope_digest,
)
from agent_comms.delivery_policy import DeliveryPolicy
from agent_comms.field_codec import FieldCodec
from agent_comms.messages import Message
from agent_comms.wire_record import WireRecord


class OriginalPublicEnvelope:
    """Borrow the certified preimage for its existing receipt validator only."""
    def __init__(self, original, promoted):
        self.original = original
        self.promoted = promoted

    @property
    def target(self):
        return self.promoted.target

    def to_wire(self):
        return self.original


class AuthoredTaskMemberPromotion:
    """The retired field belongs only to this outside-runtime transition."""

    @classmethod
    def message(cls, original_public):
        promoted = dict(original_public)
        attachment = promoted.pop('decision')
        if 'task' in promoted:
            raise ValueError('An already-target source cannot acquire a second carry.')
        promoted['task'] = attachment
        # The target declaration, not this transition, owns each valid task kind.
        return Message.from_committed_wire(promoted)

    @classmethod
    def record(cls, original_record, original_public, root_id):
        message = cls.message(original_public)
        if not has_private_wire_fields(original_record):
            return message.to_wire()
        policy = FieldCodec.decode(DeliveryPolicy, original_record[PRIVATE_WIRE_FIELD])
        initial = policy.initial
        audience = freeze_audience(
            message, initial.audience.recipients, initial.audience.source_revision,
            sender_lookup=initial.audience.sender_lookup,
            sender_name=initial.audience.sender_name,
        )
        recertified = replace(policy, initial=replace(initial, audience=audience))
        # The original decoder already validated this receipt against its own
        # Message. The receipt retains execution/key/root; only the new envelope
        # content relation changes. Policy still owns target resolution/validation.
        original_response = policy.require_receipt(
            root_id, OriginalPublicEnvelope(original_public, message))
        if original_response is not None:
            recertified = replace(recertified, response=replace(
                original_response, envelope_digest=public_envelope_digest(message.to_wire())))
        record = dict(message.to_wire(), **{PRIVATE_WIRE_FIELD: FieldCodec.encode(recertified)})
        committed = CommittedDelivery.attest(message, record, root_id)
        if committed.audience.recipients != initial.audience.recipients:
            raise ValueError('Task carry changed original addressed incarnations.')
        if committed.decisions_digest != initial.decisions_digest:
            raise ValueError('Task carry changed original delivery decisions.')
        return record

    @classmethod
    def require_target(cls, record, root_id):
        return WireRecord.from_wire(record, root_id)
