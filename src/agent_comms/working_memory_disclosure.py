"""Segment-owned disclosure selects literal addressed text, never a corpus."""
from __future__ import annotations

from typing import Annotated
from dataclasses import dataclass

from agent_comms import field_codec

from .turn_context import ContextSegment, ContextSpan
from .working_memory_questions import SpanQuestion


@dataclass(frozen=True)
class DisclosureState:
    span: str
    segment_kind: type[ContextSegment]
    source: str
    neighbor: str
    owner_rules: Annotated[tuple[str, ...], field_codec.WireOmitDefault()] = ()

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
