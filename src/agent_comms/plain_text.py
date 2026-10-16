"""Prose written about a unit says what happened in plain words.

Tristan (2026-10-16): a record of a failure must read the way you would tell him,
not in words that hide what happened. Code identifiers and quoted text are
citations, not the writer's own wording, so they are never refused.
"""

from __future__ import annotations

import re
from typing import ClassVar

from .field_codec import FieldCodec, TextRepresentation


class PlainTextError(ValueError):
    """The text is too short to say what happened, or hides it in jargon."""


class PlainText(TextRepresentation):
    """A field representation: ``Annotated[str, PlainText]`` refuses on decode and encode."""

    minimum_words: ClassVar[int] = 10
    banned_words: ClassVar[frozenset[str]] = frozenset({
        "custody", "admission", "provenance", "authority", "lattice", "surface",
        "ownership", "lifecycle", "acceptance",
    })
    banned_stems: ClassVar[tuple[str, ...]] = (
        "admit", "receipt", "checkpoint", "witness", "disposition", "invariant", "semantic",
    )
    # Quoted and backticked spans are citations of someone else's words or of code.
    _quoted: ClassVar[re.Pattern[str]] = re.compile(r'`[^`]*`|"[^"\n]*"|“[^”\n]*”')
    # Words joined by - _ / . : form one identifier (toad-close-lifecycle, goal_history.py:32).
    _token: ClassVar[re.Pattern[str]] = re.compile(r"\w+(?:[-/.:]\w+)*")

    @classmethod
    def banned(cls, word: str) -> bool:
        word = word.lower()
        return word in cls.banned_words or word.startswith(cls.banned_stems)

    @classmethod
    def from_text(cls, value: str) -> str:
        words = cls._token.findall(value)
        if len(words) < cls.minimum_words:
            raise PlainTextError(
                f"Say what actually happened in a full sentence ({cls.minimum_words}+ words).")
        # A token with anything but letters is an identifier, not the writer's wording.
        jargon = sorted({word.lower() for word in cls._token.findall(cls._quoted.sub(" ", value))
                         if word.isalpha() and cls.banned(word)})
        if jargon:
            raise PlainTextError(
                f"{', '.join(jargon)} hides what happened; say it the way you would tell Tristan.")
        return value

    @classmethod
    def encode(cls, value: object) -> str:
        return cls.from_text(FieldCodec.decode(str, value))
