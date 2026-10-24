"""Original work authority and the declared outcome of an attempt."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass

from .declared_family import DeclaredFamily
from .field_codec import DecisionKey, MessageId, RetryDecisionId, TurnId
from .turn_failure import TurnFailure


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
class VerifiedTurnDecision(WorkDecision):
    turn: TurnId

    def key(self) -> DecisionKey:
        return DecisionKey(f"verified-turn:{self.turn}")


class AttemptResolution(DeclaredFamily, affix="Resolution"):
    """Why an attempt ended, retained as the declared member."""


@dataclass(frozen=True)
class DecidedResolution(AttemptResolution):
    decision: WorkDecision


@dataclass(frozen=True)
class FailureDiagnostic(AttemptResolution):
    text: str


@dataclass(frozen=True)
class GoalCleared(AttemptResolution):
    pass


@dataclass(frozen=True)
class OperatorStopped(AttemptResolution):
    pass


@dataclass(frozen=True)
class OriginFailed(AttemptResolution):
    pass


@dataclass(frozen=True)
class UnverifiedLaunchRetired(AttemptResolution):
    pass


@dataclass(frozen=True)
class FailedTurnResolution(AttemptResolution):
    failure: type[TurnFailure]
