"""Declared Comms facts carried by the external ACP metadata envelope.

These records are shared with the paired Toad client. They describe observations;
they never authorize a native send, restore a queue, or replay an UNKNOWN input.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass

from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
from .routing import MessageRoute
from .transcripts import TranscriptCursor


class AgentCommsUpdate(DeclaredFamily, affix="Update"):
    """One declared fact; member identity supplies its wire discriminator."""


@dataclass(frozen=True)
class TurnStartedUpdate(AgentCommsUpdate):
    turn_id: str
    started_at: float | None
    activity: str | None
    activity_detail: str | None

    def __post_init__(self) -> None:
        if not self.turn_id or self.started_at is not None and self.started_at <= 0:
            raise ValueError("A started turn needs its actual ID and optional observed time")


@dataclass(frozen=True)
class TurnSettledUpdate(AgentCommsUpdate):
    turn_id: str | None


@dataclass(frozen=True)
class TextRouteUpdate(AgentCommsUpdate):
    route: MessageRoute | None


@dataclass(frozen=True)
class TranscriptChangedUpdate(AgentCommsUpdate):
    cursor: TranscriptCursor | None


class DeliveryFailure(DeclaredFamily, affix="Failure"):
    @property
    @abstractmethod
    def description(self) -> str: ...


@dataclass(frozen=True)
class BackendDeliveryFailure(DeliveryFailure):
    message: str

    @property
    def description(self) -> str:
        return self.message


@dataclass(frozen=True)
class InputFailedUpdate(AgentCommsUpdate):
    text: str
    failure: DeliveryFailure


@dataclass(frozen=True)
class UpdateBatch:
    updates: tuple[AgentCommsUpdate, ...]


def encode_updates(*updates: AgentCommsUpdate) -> dict:
    return {"agentComms": FieldCodec.encode(UpdateBatch(updates))}


def decode_updates(metadata: object) -> tuple[AgentCommsUpdate, ...]:
    """Decode facts once at ACP ingress, rejecting unknown kinds and fields."""
    if metadata is None:
        return ()
    if not isinstance(metadata, dict):
        raise ValueError("ACP metadata must be an object")
    if "agentComms" not in metadata:
        return ()
    extension = metadata["agentComms"]
    if not isinstance(extension, dict):
        raise ValueError("Comms metadata must be an object")
    if "updates" not in extension:
        return ()
    return FieldCodec.decode(UpdateBatch, extension).updates
