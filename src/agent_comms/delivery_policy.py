"""Declaration-tagged private publications and their strict boundary proofs."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import Literal, Self

from .audience_manifest import FrozenAudience
from .coordination_tables.publications import canonical_publication_key
from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import FieldCodec, MalformedRecord, NullMember, RecordMember, SubclassUnion
from .wake import ControlClassification, resolve_wake_cohort
from .wake_policy import MessageAudience, WakeDecision


class ResponseAcknowledgement(SubclassUnion, affix="ResponseAcknowledgement"):
    """A keyed response's acknowledgement, or null on an original delivery."""

    @abstractmethod
    def require(self, root_id, message) -> Self:
        """This acknowledgement, proved against its wire root and public envelope."""

    @abstractmethod
    def add_unique(self, seen_keys: set[str]) -> None: ...

    @abstractmethod
    def publication_keys(self) -> tuple[str, ...]: ...

    @abstractmethod
    def answers(self, publication_key: str, execution_id: str) -> bool:
        """Whether this acknowledges that publication; one key under another execution conflicts."""


@dataclass(frozen=True)
class NoResponseAcknowledgement(NullMember, ResponseAcknowledgement):
    def require(self, root_id, message) -> Self:
        return self

    def add_unique(self, seen_keys: set[str]) -> None:
        """An original delivery carries no response key."""

    def publication_keys(self) -> tuple[str, ...]:
        return ()

    def answers(self, publication_key: str, execution_id: str) -> bool:
        return False


@dataclass(frozen=True)
class KeyedResponseAcknowledgement(RecordMember, ResponseAcknowledgement):
    wire_root_id: str
    execution_id: str
    publication_key: str
    envelope_digest: str

    def require(self, root_id, message):
        from .bus_publication import public_envelope_digest

        if self.wire_root_id != root_id:
            raise ValueError("Response acknowledgement belongs to another wire root.")
        if self.envelope_digest != public_envelope_digest(message.to_wire()):
            raise ValueError("Response acknowledgement public envelope digest differs.")
        if self.publication_key != canonical_publication_key(self.execution_id, message.target):
            raise ValueError("Response acknowledgement publication key differs from its execution/route.")
        return self

    def add_unique(self, seen_keys: set[str]) -> None:
        if self.publication_key in seen_keys:
            raise MalformedRecord("Duplicate response publication key.")
        seen_keys.add(self.publication_key)

    def publication_keys(self) -> tuple[str, ...]:
        return (self.publication_key,)

    def answers(self, publication_key: str, execution_id: str) -> bool:
        if self.publication_key != publication_key:
            return False
        if self.execution_id != execution_id:
            raise RelationViolationError("Response publication identity conflicts.")
        return True


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
