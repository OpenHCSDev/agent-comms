"""A response's immutable recipients derive from its original committed cohorts."""

import hashlib
from dataclasses import dataclass

from .audience_manifest import FrozenRecipient, freeze_audience
from .bus_publication import CommittedDelivery
from .channel_targets import is_channel_target
from .coordination_errors import IdentityConflict
from .messages import Message
from .wake import derive_exact_reply_target


@dataclass(frozen=True)
class ResponseConversation:
    responder: FrozenRecipient
    exact_target: str
    sources: tuple[CommittedDelivery, ...]

    @classmethod
    def capture(cls, bus, snapshot):
        marker = bus.log._private_marker_unlocked()
        committed = {
            delivery.message.seq: delivery
            for record in bus.log.verified_records_unlocked(marker)
            for delivery in record.deliveries()
        }
        sources = tuple(committed[assignment.wire_seq] for assignment in snapshot.assignments)
        if not sources:
            raise IdentityConflict("Response has no committed conversation source")
        return cls(
            FrozenRecipient(snapshot.execution.owner_lookup, snapshot.execution.owner_thread),
            snapshot.execution.exact_target,
            sources,
        )

    def audience(self, message: Message):
        if message.sender != self.responder.canonical_thread:
            raise IdentityConflict("Response conversation sender changed")
        if message.target != self.exact_target:
            raise IdentityConflict("Response conversation route changed")
        members = {}
        for source in self.sources:
            if derive_exact_reply_target(source.message) != self.exact_target:
                raise IdentityConflict("Response conversation differs from original route")
            audience = source.audience
            participants = list(audience.recipients)
            if source.message.sender_role.executable:
                participants.append(FrozenRecipient(audience.sender_lookup, audience.sender_name))
            if self.responder not in audience.recipients:
                raise IdentityConflict("Responder was not in original frozen audience")
            for member in participants:
                previous = members.setdefault(member.recipient_lookup, member)
                if previous != member:
                    raise IdentityConflict("Conversation participant identity changed")
        members.pop(self.responder.recipient_lookup, None)
        recipients = tuple(members.values())
        if not is_channel_target(self.exact_target):
            recipients = tuple(m for m in recipients if m.canonical_thread == self.exact_target)
        revision = hashlib.sha256(
            "\n".join(source.audience.digest for source in self.sources).encode()
        ).hexdigest()
        return freeze_audience(
            message,
            recipients,
            revision,
            sender_lookup=self.responder.recipient_lookup,
            sender_name=self.responder.canonical_thread,
        )

    def record(self, root_id, message, intent):
        from .bus_publication import PRIVATE_WIRE_FIELD, initial_sideband, public_envelope_digest
        from .delivery_policy import KeyedResponseReceipt, ResponseDeliveryPolicy
        from .field_codec import FieldCodec
        from .wake import ControlClassification

        audience = self.audience(message)
        decisions = ResponseDeliveryPolicy.resolve(
            message, audience, ControlClassification.ORDINARY
        )
        publication = ResponseDeliveryPolicy(
            version=1,
            initial=initial_sideband(
                root_id, message, audience, decisions, control=ControlClassification.ORDINARY.value
            ),
            response=KeyedResponseReceipt(
                root_id,
                intent.execution_id,
                intent.publication_key,
                public_envelope_digest(message.to_wire()),
            ),
        )
        return {**message.to_wire(), PRIVATE_WIRE_FIELD: FieldCodec.encode(publication)}
