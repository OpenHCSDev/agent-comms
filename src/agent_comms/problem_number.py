"""The canonical spelling and numeric value of a global problem identity."""

import re
from dataclasses import dataclass
from typing import ClassVar, Self


@dataclass(frozen=True, slots=True)
class ProblemNumber:
    number: int
    prefix: ClassVar[str] = "P"
    spelling: ClassVar[re.Pattern[str]] = re.compile(rf"{prefix}[1-9][0-9]*")

    def __post_init__(self) -> None:
        if self.number < 1:
            raise ValueError("A problem number is positive")

    def __str__(self) -> str:
        return f"{self.prefix}{self.number}"

    @classmethod
    def parse(cls, text: str) -> Self:
        if not cls.accepts(text):
            raise ValueError(f"Not a canonical problem number: {text!r}")
        return cls(int(text[len(cls.prefix) :]))

    @classmethod
    def accepts(cls, text: str) -> bool:
        return bool(cls.spelling.fullmatch(text))

    @classmethod
    def sql_number(cls, column: str) -> str:
        """The numeric projection of an already validated stored identity."""
        return f"CAST(substr({column}, {len(cls.prefix) + 1}) AS INTEGER)"
