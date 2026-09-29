"""Declaration-tagged private publications and their strict boundary proofs."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import Literal

from .audience_manifest import FrozenAudience
from .coordination_tables.assignments import MessageAudience
from .coordination_tables.publications import canonical_publication_key
from .declared_family import DeclaredFamily
from .wake import ControlClassification, WakeDecision, resolve_wake_cohort
from .wake_policy import BoundedTriageWake, PassiveWake


@dataclass(frozen=True)
class KeyedResponseReceipt:
    wire_root_id: str
    execution_id: str
    publication_key: str
    envelope_digest: str

    def require(self, root_id, message):
        from .bus_publication import public_envelope_digest

        if self.wire_root_id != root_id:
            raise ValueError("Response receipt belongs to another wire root.")
        if self.envelope_digest != public_envelope_digest(message.to_wire()):
            raise ValueError("Response receipt public envelope digest differs.")
        if self.publication_key != canonical_publication_key(self.execution_id, message.target):
            raise ValueError("Response receipt publication key differs from its execution/route.")
        return self

    def add_unique(self, seen_keys: set[str]) -> None:
        if self.publication_key in seen_keys:
            raise ValueError("Duplicate response publication key.")
        seen_keys.add(self.publication_key)


@dataclass(frozen=True)
class DeliveryManifest:
    wire_root_id: str
    manifest_codec: str
    resolver_version: str
    policy_version: str
    control: ControlClassification
    audience: FrozenAudience
    # Exact existing canonical decision projection. Its digest is already bound
    # by immutable cohort receipts; changing this spelling would change proofs.
    decisions: list[dict[str, str]]
    decisions_digest: str


@dataclass(frozen=True, kw_only=True)
class DeliveryPolicy(DeclaredFamily, affix="DeliveryPolicy"):
    version: Literal[1]
    initial: DeliveryManifest

    @staticmethod
    @abstractmethod
    def resolve(message, audience, control): ...

    def require_receipt(self, root_id, message) -> KeyedResponseReceipt | None:
        return None


class InitialDeliveryPolicy(DeliveryPolicy):
    @staticmethod
    def resolve(message, audience, control):
        return resolve_wake_cohort(message, frozen_audience=audience, control=control)


@dataclass(frozen=True, kw_only=True)
class ResponseDeliveryPolicy(DeliveryPolicy):
    response: KeyedResponseReceipt

    def require_receipt(self, root_id, message) -> KeyedResponseReceipt:
        return self.response.require(root_id, message)

    @staticmethod
    def resolve(message, audience, control):
        # Informative replies allow consideration/IGNORE, including mentions
        # and DMs; they never impose a mandatory ACK ping-pong.
        mode = PassiveWake() if message.notice else BoundedTriageWake()
        return tuple(
            WakeDecision(member.recipient_lookup, MessageAudience.COLLECTIVE, mode)
            for member in audience.recipients
        )
