"""Nominal actions offered by one observed Team-board rule."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import FrozenRecordMember, TaggedRecordMember
from agent_comms.transport.tool_result import ToolResult


class BoardActionOffer(
    FrozenRecordMember,
    TaggedRecordMember,
    DeclaredFamily,
    ABC,
    affix="Offer",
):
    """One action retains its declaration and bound source identity across the UI boundary."""

    @property
    @abstractmethod
    def words(self) -> tuple[str, ...]: ...

    def text(self) -> str:
        return " ".join(self.words)

    @abstractmethod
    def execute(self, root: Path, actor: str) -> BoardActionApplied:
        """Apply this already-bound declaration as its board recipient."""


@dataclass(frozen=True)
class BoardActionApplied(ToolResult):
    lines: tuple[str, ...]
