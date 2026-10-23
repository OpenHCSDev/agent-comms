"""Inbound command contract; domain roots reuse DeclaredFamily and FieldCodec."""

from abc import ABC, abstractmethod
from typing import Any, Self

from pydantic import JsonValue

from .field_codec import FieldCodec


class Command(ABC):
    @classmethod
    def from_payload(cls, payload: dict[str, JsonValue]) -> Self:
        return FieldCodec.decode(cls, payload)

    @abstractmethod
    def apply(self, ctx: Any) -> Any:
        """Apply the command using its domain's shared preconditions."""
