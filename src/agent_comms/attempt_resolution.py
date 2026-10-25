"""Original work authority and the declared outcome of an attempt."""

from __future__ import annotations

from dataclasses import dataclass

from .declared_family import DeclaredFamily
from .field_codec import MessageId, NullMember, RecordMember, RetryDecisionId, SubclassUnion, TurnId


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


class AttemptResolution(SubclassUnion, affix="Resolution"):
    """Why an attempt ended; the absent member keeps the original null wire shape."""

    @classmethod
    def decodes_tagged_records(cls) -> bool:
        return True


class RecordedResolution(RecordMember, AttemptResolution, declared_name=None):
    """A supplied outcome keeps its original tagged record representation."""


@dataclass(frozen=True)
class NoResolution(NullMember, AttemptResolution):
    """The attempt has not recorded an outcome."""


@dataclass(frozen=True)
class DecidedResolution(RecordedResolution):
    decision: WorkDecision


@dataclass(frozen=True)
class GoalCleared(RecordedResolution):
    pass


@dataclass(frozen=True)
class OperatorStopped(RecordedResolution):
    pass


@dataclass(frozen=True)
class OriginFailed(RecordedResolution):
    pass


@dataclass(frozen=True)
class UnverifiedLaunchRetired(RecordedResolution):
    pass
