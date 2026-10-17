"""Decode retained doctrine entries; new turns use their project instructions."""

from __future__ import annotations

from dataclasses import dataclass

from ..turn_context import InstructionSegment


@dataclass(frozen=True, kw_only=True)
class DoctrineSegment(InstructionSegment):
    def values(self) -> dict[str, object]:
        return {}
