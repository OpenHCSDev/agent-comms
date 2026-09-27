"""Shared lifecycle algorithms; each domain supplies its own DeclaredFamily root.

Keep this ABC registry-free: DeclaredFamily roots own membership, and mixing it
into a root must never start a second registry for the same lifecycle.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar, Self


class LifecycleState(ABC):
    declared_name: ClassVar[str]

    @classmethod
    @abstractmethod
    def successors(cls) -> tuple[type[LifecycleState], ...]:
        """States reachable by an ordinary transition (including self, if allowed)."""

    def may_become(self, nxt: LifecycleState) -> bool:
        return type(nxt) in self.successors()

    @classmethod
    @abstractmethod
    def members_with(cls, capability: type) -> tuple[type[Self], ...]:
        """Supplied by DeclaredFamily in each domain root."""

    @classmethod
    def transition_table(cls) -> dict[str, frozenset[str]]:
        return {
            member.declared_name: frozenset(state.declared_name for state in member.successors())
            for member in cls.members_with(cls)
        }
