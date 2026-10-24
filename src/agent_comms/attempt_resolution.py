"""Original work authority and the declared outcome of an attempt."""

from __future__ import annotations

from dataclasses import dataclass

from .declared_family import DeclaredFamily
from .field_codec import MessageId, RetryDecisionId, TurnId
from .turn_failure import TurnFailure


class WorkDecision(DeclaredFamily, affix="Decision"):
    """The original authority granting new work, retained as its declared member."""


@dataclass(frozen=True)
class RetryDecision(WorkDecision):
    decision: RetryDecisionId


@dataclass(frozen=True)
class ComplianceDecision(WorkDecision):
    answer: MessageId


@dataclass(frozen=True)
class VerifiedTurnDecision(WorkDecision):
    turn: TurnId


@dataclass(frozen=True)
class OrderDecision(WorkDecision):
    order: str


class AttemptResolution(DeclaredFamily, affix="Resolution"):
    """Why an attempt ended, retained as the declared member."""


@dataclass(frozen=True)
class DecidedResolution(AttemptResolution):
    decision: WorkDecision


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
