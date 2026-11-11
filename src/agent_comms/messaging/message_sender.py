"""The original public-history sender identity, below message assembly."""

from __future__ import annotations

from dataclasses import dataclass

from agent_comms.nominal.field_codec import NullMember


@dataclass(frozen=True, slots=True)
class PublicSender(NullMember):
    """Public history has no frozen participant incarnation."""
