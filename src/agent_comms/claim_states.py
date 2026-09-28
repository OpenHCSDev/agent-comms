"""Claim decisions carry only the data valid before or after engagement."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from .coordination_errors import IntegrityViolationError
from .declared_family import DeclaredFamily
from .lifecycle import LifecycleState
from .wake_policy import BoundedTriageWake, Engagement, FullWake, PassiveWake, WakePolicy


@dataclass(frozen=True)
class PendingDecision:
    mode: WakePolicy

    def __post_init__(self):
        if not self.mode.active:
            raise IntegrityViolationError("passive decision cannot be deferred or failed")

    @property
    def verdict(self):
        return None

    @property
    def execution_id(self):
        return None

    @property
    def exact_target(self):
        return None


@dataclass(frozen=True)
class ClaimState(DeclaredFamily, LifecycleState, affix="Claim"):
    terminal: ClassVar[bool] = False
    engaged: ClassVar[bool] = False
    deferred: ClassVar[bool] = False
    triage_pending: ClassVar[bool] = False
    full_pending: ClassVar[bool] = False
    preengagement_target: ClassVar[bool] = False
    engageable: ClassVar[bool] = False
    failed: ClassVar[bool] = False
    completed: ClassVar[bool] = False

    def permits_engagement_change(self, after):
        gaining = self.execution_id is None and after.execution_id is not None
        return (not gaining or after.engaged) and not (
            self.engageable and self.execution_id is None and after.failed and gaining
        )

    @classmethod
    @abstractmethod
    def successors(cls) -> tuple[type[ClaimState], ...]: ...

    @property
    @abstractmethod
    def mode(self) -> WakePolicy: ...

    @property
    def verdict(self):
        return None

    @property
    def execution_id(self):
        return None

    @property
    def exact_target(self):
        return None

    @classmethod
    def build(cls, mode, execution_id, target):
        return cls()

    @classmethod
    def load(cls, mode, verdict, execution_id, target):
        state = cls.build(mode, execution_id, target)
        if (state.mode, state.verdict, state.execution_id) != (mode, verdict, execution_id):
            raise IntegrityViolationError("claim decision relation is inconsistent")
        if state.exact_target != target:
            raise IntegrityViolationError("claim decision target is inconsistent")
        return state

    def permits_preengagement(self, after):
        return self.execution_id is None and after.preengagement_target and self.may_become(after)


class PassiveClaim(ClaimState):
    terminal = True

    @property
    def mode(self):
        return PassiveWake()

    @classmethod
    def successors(cls):
        return ()


class TriagePendingClaim(ClaimState):
    triage_pending = True
    preengagement_target = True
    engageable = True

    @property
    def mode(self):
        return BoundedTriageWake()

    @classmethod
    def successors(cls):
        return IgnoredClaim, EngagedClaim, DeferredClaim, FailedClaim


class FullPendingClaim(ClaimState):
    full_pending = True
    preengagement_target = True
    engageable = True

    @property
    def mode(self):
        return FullWake()

    @classmethod
    def successors(cls):
        return EngagedClaim, DeferredClaim, FailedClaim


class IgnoredClaim(ClaimState):
    terminal = True
    preengagement_target = True

    @property
    def mode(self):
        return BoundedTriageWake()

    @property
    def verdict(self):
        return "ignore"

    @classmethod
    def successors(cls):
        return ()


class ClaimDecision:
    """One projection of a decision's mode, verdict and binding."""

    decision: PendingDecision | Engagement

    @property
    def mode(self):
        return self.decision.mode

    @property
    def verdict(self):
        return self.decision.verdict

    @property
    def execution_id(self):
        return self.decision.execution_id

    @property
    def exact_target(self):
        return self.decision.exact_target


@dataclass(frozen=True)
class BoundClaim(ClaimDecision, ClaimState):
    decision: Engagement

    @classmethod
    def build(cls, mode, execution_id, target):
        return cls(mode.engage(execution_id, target))


class EngagedClaim(BoundClaim):
    engaged = True

    @classmethod
    def successors(cls):
        return CompletedClaim, DeferredClaim, FailedClaim


class CompletedClaim(BoundClaim):
    terminal = True
    completed = True

    @classmethod
    def successors(cls):
        return ()


@dataclass(frozen=True)
class InterruptedClaim(ClaimDecision, ClaimState):
    decision: PendingDecision | Engagement
    preengagement_target = True

    @classmethod
    def build(cls, mode, execution_id, target):
        return cls(
            PendingDecision(mode) if execution_id is None else mode.engage(execution_id, target)
        )


class DeferredClaim(InterruptedClaim):
    deferred = True
    engageable = True

    @classmethod
    def successors(cls):
        return TriagePendingClaim, FullPendingClaim, EngagedClaim, FailedClaim

    def permits_engagement_change(self, after):
        return super().permits_engagement_change(after) and not (
            self.execution_id is not None and (after.triage_pending or after.full_pending)
        )


class FailedClaim(InterruptedClaim):
    terminal = True
    failed = True

    @classmethod
    def successors(cls):
        return ()
