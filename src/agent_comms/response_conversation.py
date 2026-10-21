"""A response's immutable recipients derive from its original committed cohorts."""

import hashlib
from dataclasses import dataclass, replace

from agent_comms.response_acknowledgement import (
    KeyedResponseAcknowledgement,
    public_envelope_digest,
)

from .audience_manifest import FrozenRecipient, freeze_audience
from .bus_publication import CommittedDelivery
from .channel_targets import CommandTarget
from .coordination_errors import IdentityConflict
from .coordination_tables.responses import ResponseObligation
from .message_reference import MessageReference
from .messages import Message


@dataclass(frozen=True)
class ResponseConversation:
    responder: FrozenRecipient
    obligation: ResponseObligation
    sources: tuple[CommittedDelivery, ...]

    @classmethod
    def source_references(cls, snapshot, obligation: ResponseObligation) -> tuple[MessageReference, ...]:
        """The selected original messages belonging to this exact reply route."""
        if obligation.execution_id != snapshot.execution.execution_id:
            raise IdentityConflict("Response obligation belongs to another execution")
        originals = tuple(
            assignment.source for assignment in snapshot.assignments
            if assignment.lifecycle.exact_target == obligation.exact_target
        )
        if not originals:
            raise IdentityConflict("Response obligation has no original conversation")
        return originals

    @classmethod
    def answer_references(
        cls, snapshot, obligation: ResponseObligation, following: tuple[MessageReference, ...]
    ) -> tuple[MessageReference, ...]:
        return tuple(dict.fromkeys((*cls.source_references(snapshot, obligation), *following)))

    @classmethod
    def capture(cls, bus, snapshot, obligation: ResponseObligation):
        originals = cls.source_references(snapshot, obligation)
        marker = bus.log.private_marker_unlocked()
        sources = bus.log.delivery_cohorts_unlocked(
            marker.root_id, tuple(reference.seq for reference in originals)
        )
        if len(sources) != len(originals):
            raise IdentityConflict("Response obligation has no complete original conversation")
        if {source.message.reference for source in sources} != set(originals):
            raise IdentityConflict("Response conversation lost an original source")
        if any(source.message.exact_reply_target != obligation.exact_target for source in sources):
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
            if source.message.exact_reply_target != self.obligation.exact_target:
                raise IdentityConflict("Response conversation differs from original route")
            audience = source.audience
            participants = list(audience.recipients)
            participants.extend(source.message.sender_role.executor(
                FrozenRecipient(audience.sender_lookup, audience.sender_name)
            ))
            if self.responder not in audience.recipients:
                raise IdentityConflict("Responder was not in original frozen audience")
            for member in participants:
                previous = members.setdefault(member.recipient_lookup, member)
                if previous != member:
                    raise IdentityConflict("Conversation participant identity changed")
        members.pop(self.responder.recipient_lookup, None)
        recipients = CommandTarget.named(self.obligation.exact_target).response_recipients(tuple(members.values()))
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

    def record(self, seq: int, message: Message, intent):
        from agent_comms.bus import KeyedPrivateRecord
        from agent_comms.delivery_policy import ResponseDeliveryPolicy
        from agent_comms.wake import ControlClassification

        stored = replace(message, seq=seq)
        audience = self.audience(stored)
        decisions = ResponseDeliveryPolicy.resolve(
            stored, audience, ControlClassification.ORDINARY,
        )
        return KeyedPrivateRecord.from_message(
            stored, audience=audience, decisions=decisions,
            control=ControlClassification.ORDINARY.value,
            acknowledgement=KeyedResponseAcknowledgement(
                self.sources[0].wire_root_id, intent.execution_id, intent.publication_key,
                public_envelope_digest(stored.to_wire()),
            ),
        )
