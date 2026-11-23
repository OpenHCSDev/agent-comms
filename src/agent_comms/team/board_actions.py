"""Nominal actions offered by one observed Team-board rule."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import FrozenRecordMember, TaggedRecordMember
from agent_comms.transport.tool_result import ToolResult

if TYPE_CHECKING:
    from agent_comms.registry.thread_identity import ThreadIncarnation


class BoardActionOffer(
    FrozenRecordMember,
    TaggedRecordMember,
    DeclaredFamily,
    affix="Offer",
):
    """One action retains its declaration and bound source identity across the UI boundary."""

    @property
    @abstractmethod
    def label(self) -> str: ...

    @property
    @abstractmethod
    def words(self) -> tuple[str, ...]: ...

    def text(self) -> str:
        return " ".join(self.words)

    def bind(self, recipient: ThreadIncarnation) -> BoardActionOffer:
        """Bind this recipient-neutral action to the board incarnation executing it."""
        return self if getattr(self, "recipient", None) == recipient else replace(
            self, recipient=recipient,
        )

    @abstractmethod
    def execute(self, root: Path, actor: str) -> BoardActionApplied:
        """Apply this already-bound declaration as its board recipient."""


@dataclass(frozen=True)
class BoardActionApplied(ToolResult):
    lines: tuple[str, ...]
