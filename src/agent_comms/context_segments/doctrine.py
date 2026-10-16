"""Maintenance rules in the native session's system layer, never turn history."""

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
