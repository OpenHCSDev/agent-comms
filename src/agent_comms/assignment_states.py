"""Claim decisions carry only the data valid before or after engagement."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from .coordination_errors import IdentityConflict, IntegrityViolationError
from .declared_family import DeclaredFamily
from .lifecycle import LifecycleState
from .typed_table import sql_literal
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
    def requires_selected_triage(self) -> bool:
        if self.triage_pending:
            return True
        if self.mode.triage:
            raise IdentityConflict("pending claim wake decision is not executable")
        return False

    def require_engagement(self) -> Engagement:
        raise IdentityConflict("wake assignment is not engaged")

    def require_completion(self) -> Engagement:
        raise IdentityConflict("wake assignment is not completed")

    notification_state: ClassVar[str] = "Pending"
    notification_detail: ClassVar[str] = "Awaiting a recorded notification outcome."
    notification_priority: ClassVar[int] = 3
    notification_busy: ClassVar[bool] = False

    def notification(
        self,
        recipient: str,
        *,
        owner_active: bool,
        current_turn: bool = False,
        triage_inflight: bool = False,
        blocked_by_prior: bool = False,
        prior_turn_active: bool = False,
    ):
        from .presentation import MessageNotification

        if not owner_active and (self.triage_pending or self.full_pending):
            return MessageNotification(
                recipient,
                "Waiting for agent",
                "Agent is stopped; this message has not been checked.",
                priority=4,
            )
        if blocked_by_prior and (self.triage_pending or self.full_pending):
            return MessageNotification(
                recipient,
                "Queued behind current turn" if prior_turn_active else "Blocked by earlier turn",
                "The agent is finishing an earlier turn; this message has not started."
                if prior_turn_active
                else "An earlier turn has an unresolved outcome. This message is saved "
                "and has not started; the earlier turn needs recovery, not a resend.",
            )
        return MessageNotification(
            recipient,
            self.notification_state,
            self.notification_detail,
            self.notification_priority,
            self.notification_busy,
        )

    terminal: ClassVar[bool] = False
    engaged: ClassVar[bool] = False
    deferred: ClassVar[bool] = False
    triage_pending: ClassVar[bool] = False
    full_pending: ClassVar[bool] = False
    preengagement_target: ClassVar[bool] = False
    engageable: ClassVar[bool] = False
    failed: ClassVar[bool] = False
    completed: ClassVar[bool] = False

    @classmethod
    def mode_expression(cls, expression: str) -> str:
        return sql_literal(cls().mode.declared_name)

    @classmethod
    def verdict_expression(cls, expression: str) -> str:
        return sql_literal(cls().verdict)

    @classmethod
    def binding_expression(cls) -> str:
        return "execution_id IS NULL AND exact_target IS NULL"

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
    notification_priority = 2
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

    @classmethod
    def mode_expression(cls, expression: str) -> str:
        return (
            f"coalesce(json_extract({expression}, '$.decision.mode.kind'), "
            f"json_extract({expression}, '$.decision.kind'))"
        )

    @classmethod
    def verdict_expression(cls, expression: str) -> str:
        return Engagement.verdict_expression(f"json_extract({expression}, '$.decision')")

    @classmethod
    def binding_expression(cls) -> str:
        active = ",".join(
            sql_literal(member.declared_name)
            for member in WakePolicy.members_with(WakePolicy)
            if member.active
        )
        return (
            f"wake_mode IN ({active}) AND ((execution_id IS NULL AND exact_target IS NULL) "
            "OR (execution_id IS NOT NULL AND exact_target IS NOT NULL))"
        )

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
    def binding_expression(cls) -> str:
        return "execution_id IS NOT NULL AND exact_target IS NOT NULL"

    @classmethod
    def build(cls, mode, execution_id, target):
        return cls(mode.engage(execution_id, target))


class EngagedAssignment(BoundAssignment):
    def require_engagement(self) -> Engagement:
        return self.decision

    def notification(
        self,
        recipient,
        *,
        owner_active,
        current_turn=False,
        triage_inflight=False,
        blocked_by_prior=False,
        prior_turn_active=False,
    ):
        from .presentation import MessageNotification

        if not current_turn:
            return MessageNotification(
                recipient,
                "Paused",
                "A response was selected, but no matching active turn is running. "
                "Outcome is unconfirmed; do not automatically retry.",
            )
        return super().notification(recipient, owner_active=owner_active)

    notification_state = "Responding…"
    notification_detail = "The agent chose to respond; work is in progress."
    notification_priority = 1
    notification_busy = True

    engaged = True

    @classmethod
    def successors(cls):
        return CompletedAssignment, DeferredAssignment, FailedAssignment


class CompletedAssignment(BoundAssignment):
    def require_completion(self) -> Engagement:
        return self.decision

    notification_priority = 2
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
    def notification(
        self,
        recipient,
        *,
        owner_active,
        current_turn=False,
        triage_inflight=False,
        blocked_by_prior=False,
        prior_turn_active=False,
    ):
        from .presentation import MessageNotification

        if current_turn and triage_inflight:
            return MessageNotification(
                recipient,
                "Checking relevance…",
                "Checking relevance before deciding whether to respond.",
                priority=0,
                busy=True,
            )
        if triage_inflight:
            return MessageNotification(
                recipient,
                "Outcome uncertain",
                "The check was attempted but no decision was confirmed. "
                "Do not automatically retry.",
            )
        return super().notification(recipient, owner_active=owner_active)

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
