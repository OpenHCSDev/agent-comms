"""Declaration-tagged private publications and their strict boundary proofs."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import Literal

from agent_comms.audience_manifest import FrozenAudience
from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec
from agent_comms.response_acknowledgement import (
    KeyedResponseAcknowledgement, NoResponseAcknowledgement, ResponseAcknowledgement,
)
from agent_comms.wake import ControlClassification, resolve_wake_cohort
from agent_comms.wake_policy import MessageAudience, WakeDecision


@dataclass(frozen=True)
class DeliveryManifest:
    wire_root_id: str
    manifest_codec: str
    resolver_version: str
    policy_version: str
    control: ControlClassification
    audience: FrozenAudience
    # Exact existing canonical decision projection. Its digest is already bound
    # by immutable cohort evidences; changing this spelling would change proofs.
    decisions: list[dict[str, str]]
    decisions_digest: str


@dataclass(frozen=True, kw_only=True)
class DeliveryPolicy(DeclaredFamily, affix="DeliveryPolicy"):
    version: Literal[1]
    initial: DeliveryManifest

    @classmethod
    def from_sideband(cls, value: object) -> DeliveryPolicy:
        """The declared policy a committed row's private sideband names."""
        return FieldCodec.decode(cls, value)

    @staticmethod
    @abstractmethod
    def resolve(message, audience, control): ...

    def require_acknowledgement(self, root_id, message) -> ResponseAcknowledgement:
        return NoResponseAcknowledgement()

    @property
    def acknowledgement(self) -> ResponseAcknowledgement:
        return NoResponseAcknowledgement()


class InitialDeliveryPolicy(DeliveryPolicy):
    @staticmethod
    def resolve(message, audience, control):
        return resolve_wake_cohort(message, frozen_audience=audience, control=control)


@dataclass(frozen=True, kw_only=True)
class ResponseDeliveryPolicy(DeliveryPolicy):
    response: KeyedResponseAcknowledgement

    def require_acknowledgement(self, root_id, message) -> KeyedResponseAcknowledgement:
        return self.response.require(root_id, message)

    @property
    def acknowledgement(self) -> KeyedResponseAcknowledgement:
        return self.response

    @staticmethod
    def resolve(message, audience, control):
        # Informative replies allow consideration/IGNORE, including mentions
        # and DMs; they never impose a mandatory ACK ping-pong.
        mode = message.role.response_wake_mode()
        return tuple(
            WakeDecision(member.recipient_lookup, MessageAudience.COLLECTIVE, mode)
            for member in audience.recipients
        )
