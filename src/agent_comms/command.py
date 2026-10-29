"""Inbound command contract; domain roots reuse DeclaredFamily and FieldCodec."""

from abc import ABC, abstractmethod
from typing import Self

from collections.abc import Mapping

from agent_comms.nominal.field_codec import FieldCodec


class Command[Context, Result](ABC):
    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> Self:
        return FieldCodec.decode(cls, payload)

    @abstractmethod
    def apply(self, ctx: Context) -> Result:
        """Apply the command using its domain's shared preconditions."""
