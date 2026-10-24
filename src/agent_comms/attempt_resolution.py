"""Original work authority and attempt outcomes, with their historical text spelling."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from .declared_family import DeclaredFamily
from .field_codec import DecisionKey, MessageId, RetryDecisionId, TurnId
from .turn_failure import TurnFailure

if TYPE_CHECKING:
    from .todos import PendingProblemOrder


class WorkDecision(DeclaredFamily, affix="Decision"):
    """The writer knows which authority grants new work; storage holds its opaque key."""

    @abstractmethod
    def key(self) -> DecisionKey: ...


@dataclass(frozen=True)
class RetryDecision(WorkDecision):
    decision: RetryDecisionId

    def key(self) -> DecisionKey:
        return DecisionKey(self.decision)


@dataclass(frozen=True)
class ComplianceDecision(WorkDecision):
    answer: MessageId

    def key(self) -> DecisionKey:
        return DecisionKey(self.answer)


@dataclass(frozen=True)
class OrderDecision(WorkDecision):
    order: PendingProblemOrder

    def key(self) -> DecisionKey:
        return DecisionKey(self.order.id)


@dataclass(frozen=True)
class VerifiedTurnDecision(WorkDecision):
    turn: TurnId

    def key(self) -> DecisionKey:
        return DecisionKey(f"verified-turn:{self.turn}")


class AttemptResolution(DeclaredFamily, affix="Resolution"):
    """The reason is typed by its writer; its historical column stays plain text."""

    @abstractmethod
    def recorded(self) -> str: ...


@dataclass(frozen=True)
class DecidedResolution(AttemptResolution):
    decision: WorkDecision

    def recorded(self) -> str:
        return str(self.decision.key())


@dataclass(frozen=True)
class FailureDiagnostic(AttemptResolution):
    text: str

    def recorded(self) -> str:
        return self.text


@dataclass(frozen=True)
class GoalCleared(FailureDiagnostic):
    text: ClassVar[str] = "goal cleared"


@dataclass(frozen=True)
class OperatorStopped(FailureDiagnostic):
    text: ClassVar[str] = "Operator stopped goal turn."


@dataclass(frozen=True)
class OriginFailed(FailureDiagnostic):
    text: ClassVar[str] = "Goal origin turn did not finish successfully."


@dataclass(frozen=True)
class UnverifiedLaunchRetired(FailureDiagnostic):
    text: ClassVar[str] = "Launch claim retired without verified terminal progress."


@dataclass(frozen=True)
class FailedTurnResolution(AttemptResolution):
    kind: type[TurnFailure]

    def recorded(self) -> str:
        return self.kind.declared_name


