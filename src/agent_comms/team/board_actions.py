"""Nominal actions offered by one observed Team-board rule."""

from __future__ import annotations

from abc import ABC, abstractmethod

from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import FrozenRecordMember, TaggedRecordMember


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
