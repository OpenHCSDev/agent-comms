"""Maintenance doctrine injected into every comms agent's turn context.

The declared source file owns the results and the confirmed worked cases;
this segment derives its text from those bytes and owns the injection.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..turn_context import InstructionFile, InstructionSegment


@dataclass(frozen=True, kw_only=True)
class DoctrineSegment(InstructionSegment):
    @classmethod
    def capture(cls) -> DoctrineSegment:
        instruction = InstructionFile.read("doctrine.md")
        return cls(provenance=(instruction.source,), instruction=instruction)

    def values(self) -> dict[str, object]:
        return {}
