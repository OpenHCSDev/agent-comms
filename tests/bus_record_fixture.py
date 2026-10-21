"""Typed bus values without importing a test module and collecting its cases."""

from agent_comms.audience_manifest import FrozenRecipient, freeze_audience
from agent_comms.bus_publication import Delivered, KeyedPublication
from agent_comms.envelope_claim_transitions import UnscopedClaims
from agent_comms.messages import ClaimMessage
from agent_comms.response_acknowledgement import (
    KeyedResponseAcknowledgement, canonical_publication_key, public_envelope_digest,
)
from agent_comms.wake import ControlClassification, resolve_wake_cohort


def record_from(declaration, message):
    capabilities = {}
    if issubclass(declaration, Delivered):
        audience = freeze_audience(
            message,
            (FrozenRecipient("b" * 32, message.target),),
            "c" * 64,
            sender_lookup="a" * 32,
            sender_name=message.sender,
        )
        capabilities.update(
            audience=audience,
            decisions=resolve_wake_cohort(
                message, frozen_audience=audience, control=ControlClassification.ORDINARY
            ),
            control=ControlClassification.ORDINARY.value,
        )
    if issubclass(declaration, KeyedPublication):
        capabilities["acknowledgement"] = KeyedResponseAcknowledgement(
            "d" * 32,
            "e" * 32,
            canonical_publication_key("e" * 32, message.target),
            public_envelope_digest(message.to_wire()),
        )
    if issubclass(declaration, ClaimMessage):
        return declaration.of(
            message,
            UnscopedClaims(
                owner=message.sender,
                incarnation="1",
                seq=message.seq,
                message_id=message.message_id,
                claims=("/owned/resource",),
                generation="f" * 32,
            ),
        )
    return declaration.from_message(message, **capabilities)
