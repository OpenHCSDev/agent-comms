"""Segment-owned disclosure selects literal addressed text, never a corpus."""
from __future__ import annotations

import re
from abc import abstractmethod
from dataclasses import dataclass, field

from .declared_family import DeclaredFamily
from .turn_context import ContextSegment, ContextSpan
from .working_memory_questions import SpanQuestion


class DisclosurePolicy(DeclaredFamily, affix="Disclosure"):
    @abstractmethod
    def require_text(self, text: str) -> str: ...


@dataclass(frozen=True)
class WithheldDisclosure(DisclosurePolicy):
    def require_text(self, text: str) -> str:
        raise ValueError("This original segment does not permit third-party disclosure")


@dataclass(frozen=True)
class PublicInstructionDisclosure(DisclosurePolicy):
    # Credential-shaped values are withheld as a whole. Replacing them would
    # make the classifier label prose which is not the addressed original span.
    credential = re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:sk|ghp|github_pat)[-_][A-Za-z0-9_-]+"
        r"|\b(?:api[_ -]?key|access[_ -]?token|password|secret)\s*[:=]\s*\S+"
        r"|\bBearer\s+\S+", re.IGNORECASE)

    def require_text(self, text: str) -> str:
        if self.credential.search(text):
            raise ValueError("Credential-shaped original text cannot be disclosed")
        return text


@dataclass(frozen=True)
class DisclosureState:
    span: str
    segment_kind: type[ContextSegment]
    source: str
    neighbor: str
    owner_rules: tuple[str, ...] = field(default=(), metadata={"wire_omit_default": True})

    @classmethod
    def capture(cls, segment: ContextSegment, span: ContextSpan, question: type[SpanQuestion],
                owner_rules: tuple[str, ...] = ()):
        original = segment.public_spans()
        try:
            index = original.index(span)
        except ValueError:
            raise ValueError("Disclosure names no original segment span") from None
        policy = segment.disclosure_for(span)
        neighbor = original[index - 1].public_text(segment) if index else ""
        # The neighbor must independently be allowed; tool/file text cannot
        # escape through adjacency to an allowed agent message.
        if index:
            neighbor = segment.disclosure_for(original[index - 1]).require_text(neighbor)
        return cls(policy.require_text(span.public_text(segment)), type(segment),
                   policy.require_text("; ".join(source.public_description()
                                                for source in span.coordinates.provenance)),
                   neighbor, question.disclosed_owner_rules(owner_rules))
