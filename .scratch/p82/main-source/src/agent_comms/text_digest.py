"""One identity and encoding for exact UTF-8 text content."""

import hashlib
import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TextDigest:
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or re.fullmatch(r"[0-9a-f]{64}", self.value) is None:
            raise ValueError("Text digest must be a SHA-256 hex value")

    @classmethod
    def of(cls, text: str) -> "TextDigest":
        return cls(hashlib.sha256(text.encode("utf-8")).hexdigest())

    def matches(self, text: str) -> bool:
        return self == self.of(text)
