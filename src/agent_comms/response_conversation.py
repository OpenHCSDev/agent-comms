"""A response's immutable recipients derive from its original committed cohorts."""

import hashlib
from dataclasses import dataclass

from .audience_manifest import FrozenRecipient, freeze_audience
from .bus_publication import CommittedDelivery
from .channel_targets import is_channel_target
from .coordination_errors import IdentityConflict
from .messages import Message
from .coordination_tables.responses import ResponseObligation
from .wake import derive_exact_reply_target


@dataclass(frozen=True)
class ResponseConversation:
    responder: FrozenRecipient
    obligation: ResponseObligation
    sources: tuple[CommittedDelivery, ...]

    @classmethod
    def capture(cls, bus, snapshot, obligation: ResponseObligation):
        if obligation.execution_id != snapshot.execution.execution_id:
            raise IdentityConflict("Response obligation belongs to another execution")
        marker = bus.log._private_marker_unlocked()
        assignments = tuple(
            assignment for assignment in snapshot.assignments
            if assignment.lifecycle.exact_target == obligation.exact_target
        )
        sources = bus.log.delivery_cohorts_unlocked(
            marker.root_id, tuple(assignment.wire_seq for assignment in assignments)
        )
        if not sources or len(sources) != len(assignments):
            raise IdentityConflict("Response obligation has no complete original conversation")
        originals = {assignment.source for assignment in assignments}
        if {source.message.reference for source in sources} != originals:
            raise IdentityConflict("Response conversation lost an original source")
        if any(derive_exact_reply_target(source.message) != obligation.exact_target
               for source in sources):
            raise IdentityConflict("Response obligation differs from its original routes")
        return cls(
            FrozenRecipient(snapshot.execution.owner_lookup, snapshot.execution.owner_thread),
            obligation,
            sources,
        )

    def audience(self, message: Message):
        if message.sender != self.responder.canonical_thread:
            raise IdentityConflict("Response conversation sender changed")
        if message.target != self.obligation.exact_target:
            raise IdentityConflict("Response conversation route changed")
        members = {}
        for source in self.sources:
            if derive_exact_reply_target(source.message) != self.obligation.exact_target:
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
        if not is_channel_target(self.obligation.exact_target):
            recipients = tuple(m for m in recipients if m.canonical_thread == self.obligation.exact_target)
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
