"""One identity and encoding for exact UTF-8 text content."""

import hashlib
from dataclasses import dataclass

from .field_codec import Sha256Text


@dataclass(frozen=True, slots=True)
class TextDigest:
    value: str

    def __post_init__(self) -> None:
        Sha256Text.decode(self.value)

    @classmethod
    def of(cls, text: str) -> "TextDigest":
        return cls(hashlib.sha256(text.encode("utf-8")).hexdigest())

    def matches(self, text: str) -> bool:
        return self == self.of(text)
