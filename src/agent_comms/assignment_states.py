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
class AssignmentState(DeclaredFamily, LifecycleState, affix="Assignment"):
    notification_state: ClassVar[str] = "Pending"
    notification_detail: ClassVar[str] = "Awaiting a recorded notification outcome."

    def notification(
        self, *, owner_active: bool, current_turn: bool = False, triage_inflight: bool = False
    ) -> tuple[str, str]:
        if not owner_active and (self.triage_pending or self.full_pending):
            return "Waiting for agent", "Agent is stopped; this message has not been checked."
        return self.notification_state, self.notification_detail

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
    def successors(cls) -> tuple[type[AssignmentState], ...]: ...

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


class PassiveAssignment(AssignmentState):
    notification_state = "Passive"
    notification_detail = "Available as context; no automatic response requested."

    terminal = True

    @property
    def mode(self):
        return PassiveWake()

    @classmethod
    def successors(cls):
        return ()


class TriagePendingAssignment(AssignmentState):
    notification_state = "Pending"
    notification_detail = "Queued for a bounded relevance check; not yet checked."

    triage_pending = True
    preengagement_target = True
    engageable = True

    @property
    def mode(self):
        return BoundedTriageWake()

    @classmethod
    def successors(cls):
        return IgnoredAssignment, EngagedAssignment, DeferredAssignment, FailedAssignment


class FullPendingAssignment(AssignmentState):
    notification_state = "Pending"
    notification_detail = "Queued for an agent response; not yet started."

    full_pending = True
    preengagement_target = True
    engageable = True

    @property
    def mode(self):
        return FullWake()

    @classmethod
    def successors(cls):
        return EngagedAssignment, DeferredAssignment, FailedAssignment


class IgnoredAssignment(AssignmentState):
    notification_state = "Checked — no response"
    notification_detail = "The agent checked this message and chose not to respond."

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


class AssignmentDecision:
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
class BoundAssignment(AssignmentDecision, AssignmentState):
    decision: Engagement

    @classmethod
    def build(cls, mode, execution_id, target):
        return cls(mode.engage(execution_id, target))


class EngagedAssignment(BoundAssignment):
    def notification(self, *, owner_active, current_turn=False, triage_inflight=False):
        if not current_turn:
            return "Paused", "A response was selected, but no matching active turn is running."
        return super().notification(owner_active=owner_active)

    notification_state = "Responding"
    notification_detail = "The agent chose to respond; work is in progress."

    engaged = True

    @classmethod
    def successors(cls):
        return CompletedAssignment, DeferredAssignment, FailedAssignment


class CompletedAssignment(BoundAssignment):
    notification_state = "Responded"
    notification_detail = "The response workflow completed."

    terminal = True
    completed = True

    @classmethod
    def successors(cls):
        return ()


@dataclass(frozen=True)
class InterruptedAssignment(AssignmentDecision, AssignmentState):
    decision: PendingDecision | Engagement
    preengagement_target = True

    @classmethod
    def build(cls, mode, execution_id, target):
        return cls(
            PendingDecision(mode) if execution_id is None else mode.engage(execution_id, target)
        )


class DeferredAssignment(InterruptedAssignment):
    def notification(self, *, owner_active, current_turn=False, triage_inflight=False):
        if current_turn and triage_inflight:
            return "Checking", "Checking relevance before deciding whether to respond."
        return super().notification(owner_active=owner_active)

    notification_state = "Paused"
    notification_detail = "Processing was deferred; this is not a successful receipt."

    deferred = True
    engageable = True

    @classmethod
    def successors(cls):
        return TriagePendingAssignment, FullPendingAssignment, EngagedAssignment, FailedAssignment

    def permits_engagement_change(self, after):
        return super().permits_engagement_change(after) and not (
            self.execution_id is not None and (after.triage_pending or after.full_pending)
        )


class FailedAssignment(InterruptedAssignment):
    notification_state = "Failed"
    notification_detail = "Notification processing failed; inspect the agent error before retrying."

    terminal = True
    failed = True

    @classmethod
    def successors(cls):
        return ()
