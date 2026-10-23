"""Inbound command contract; domain roots reuse DeclaredFamily and FieldCodec."""

from abc import ABC, abstractmethod
from typing import Self

from pydantic import JsonValue

from .field_codec import FieldCodec


class Command[Context, Result](ABC):
    @classmethod
    def from_payload(cls, payload: dict[str, JsonValue]) -> Self:
        return FieldCodec.decode(cls, payload)

    @abstractmethod
    def apply(self, ctx: Context) -> Result:
        """Apply the command using its domain's shared preconditions."""
