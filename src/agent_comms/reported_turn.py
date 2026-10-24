"""The turn's reporting identity, shared by turn and goal owners."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass

from .declared_family import DeclaredFamily
from .field_codec import JsonShapeFamily, JsonShapeMember, TurnId


class ReportedTurnState(DeclaredFamily, JsonShapeFamily, affix="ReportedTurnState"):
    """The reporting turn is a state; null/text remain the existing wire forms."""

    @abstractmethod
    def reports(self, turn: ReportedTurnState) -> bool: ...


@dataclass(frozen=True)
class NoReportedTurn(ReportedTurnState, JsonShapeMember):
    value: None = None

    def reports(self, turn: ReportedTurnState) -> bool:
        return False


@dataclass(frozen=True)
class ReportedTurn(ReportedTurnState, JsonShapeMember):
    value: TurnId

    def reports(self, turn: ReportedTurnState) -> bool:
        return turn == self
