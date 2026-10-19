"""Claim decisions carry only the data valid before or after engagement."""

from __future__ import annotations

import json
from abc import abstractmethod
from dataclasses import dataclass, replace
from typing import ClassVar, TYPE_CHECKING

from .coordination_errors import IdentityConflict, IntegrityViolationError
from .declared_family import DeclaredFamily
from .lifecycle import LifecycleState
from .notification_progress import (
    UnrecordedProgress, ContextualProgress, PassiveProgress, IgnoredProgress,
    EngagedProgress, CompletedProgress, InterruptedProgress, FailedProgress,
)
from .typed_table import sql_literal
from .wake_policy import BoundedTriageWake, Engagement, FullWake, PassiveWake, WakePolicy

if TYPE_CHECKING:
    from .agent_activity import RecipientActivity


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
    @classmethod
    def selection_predicate(cls, capability: type, alias: str) -> str:
        states = ",".join(
            sql_literal(member) for member in cls.members_with(capability)
        )
        return capability.extend_selection(
            f"{alias}.disposition IN ({states})", alias
        )

    @classmethod
    def extend_selection(cls, predicate: str, alias: str) -> str:
        return predicate

    def rejected_triage_history(self, execution, **source):
        raise ValueError("Recorded triage lacks a decision or a terminal failed claim")

    def wake_frame(self, source, obligation) -> tuple[str, str]:
        raise IdentityConflict("full wake frame requires the current response obligation")

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

    notification_state: ClassVar[type[ContextualProgress]] = ContextualProgress
    notification_changed: ClassVar[bool] = False
    records_conversation_response: ClassVar[bool] = False

    def recorded_response(self, original, replies, recipient_lookup):
        return None

    def notification(
        self,
        recipient,
        sender_lookup,
        *,
        observation: RecipientActivity,
        activity=None,
        updated_at_ms: int = 0,
        triage_inflight: bool = False,
    ):
        from .presentation import MessageNotification

        return MessageNotification(
            recipient,
            sender_lookup,
            self.notification_state(UnrecordedProgress() if activity is None else activity.progress),
            changed=self.notification_changed,
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
        return sql_literal(type(cls().mode))

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

    @classmethod
    def build_preengagement(cls, mode: WakePolicy) -> AssignmentState:
        if cls.preengagement_target:
            return cls.build(mode, None, None)
        raise IdentityConflict("preengagement transition is not declared")

    def preengagement(self, disposition: type[AssignmentState]) -> AssignmentState:
        if self.execution_id is not None or disposition not in self.successors():
            raise IdentityConflict("preengagement transition is not declared")
        after = disposition.build_preengagement(self.mode)
        if after.mode != self.mode:
            raise IdentityConflict("preengagement cannot change frozen wake policy")
        return after


class PassiveAssignment(AssignmentState):
    notification_state = PassiveProgress

    terminal = True

    @property
    def mode(self):
        return PassiveWake()

    @classmethod
    def successors(cls):
        return ()


class PendingNotification:
    """Pending relevance/full execution membership, not another display state."""

    @classmethod
    def extend_selection(cls, predicate: str, alias: str) -> str:
        # A deferred assignment already linked to an execution may be selected
        # for that execution's next attempt after replay was checked. UNKNOWN
        # input without an execution link is never selected.
        deferred = sql_literal(DeferredAssignment)
        return (
            f"({predicate} OR ({alias}.disposition={deferred} "
            f"AND {alias}.execution_id IS NOT NULL))"
        )

    def notification(
        self,
        recipient,
        sender_lookup,
        *,
        observation,
        activity=None,
        updated_at_ms=0,
        triage_inflight=False,
    ):
        notification = super().notification(
            recipient, sender_lookup, observation=observation, activity=activity
        )
        return (
            notification
            if activity is None
            else activity.pending_notification(notification, updated_at_ms)
        )


class SelectedResponseNotification:
    """An engaged or deferred decision shows its selected response's progress."""

    def notification(
        self,
        recipient,
        sender_lookup,
        *,
        observation,
        activity,
        updated_at_ms=0,
        triage_inflight=False,
    ):
        notification = super().notification(
            recipient, sender_lookup, observation=observation, activity=activity
        )
        return observation.selected_response(
            notification, updated_at_ms, activity.progress
        )


class TriagePendingAssignment(PendingNotification, AssignmentState):
    def wake_frame(self, source, obligation) -> tuple[str, str]:
        if obligation is not None:
            raise IdentityConflict("bounded triage cannot inherit a response obligation")
        return self.mode.triage_expectation(), "No response obligation exists until triage engages."


    triage_pending = True
    preengagement_target = True
    engageable = True

    @property
    def mode(self):
        return BoundedTriageWake()

    @classmethod
    def successors(cls):
        return IgnoredAssignment, EngagedAssignment, DeferredAssignment, FailedAssignment


class FullPendingAssignment(PendingNotification, AssignmentState):

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
    notification_changed = True
    notification_state = IgnoredProgress

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
            sql_literal(member)
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
    notification_changed = True
    decision: Engagement

    @classmethod
    def binding_expression(cls) -> str:
        return "execution_id IS NOT NULL AND exact_target IS NOT NULL"

    @classmethod
    def build(cls, mode, execution_id, target):
        return cls(mode.engage(execution_id, target))


class EngagedAssignment(SelectedResponseNotification, BoundAssignment):
    notification_state = EngagedProgress
    def wake_frame(self, source, obligation) -> tuple[str, str]:
        if obligation is None:
            raise IdentityConflict("full wake frame requires the current response obligation")
        obligation.lifecycle.require_preparation()
        if (obligation.execution_id, obligation.exact_target) != (
            self.decision.execution_id,
            source.exact_reply_target,
        ):
            raise IdentityConflict("full wake frame requires the current response obligation")
        return self.mode.full_expectation(), "you owe a response: " + json.dumps(
            {
                "target": obligation.exact_target,
                "source_seq": source.seq,
                "execution_id": obligation.execution_id,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )

    def require_engagement(self) -> Engagement:
        return self.decision


    engaged = True

    @classmethod
    def successors(cls):
        return CompletedAssignment, DeferredAssignment, FailedAssignment


class CompletedAssignment(BoundAssignment):
    def notification(
        self,
        recipient,
        sender_lookup,
        *,
        observation,
        activity,
        updated_at_ms=0,
        triage_inflight=False,
    ):
        notification = super().notification(
            recipient, sender_lookup, observation=observation, activity=activity
        )
        return activity.completed_notification(notification)

    def require_completion(self) -> Engagement:
        return self.decision

    notification_state = CompletedProgress

    terminal = True
    completed = True

    @classmethod
    def successors(cls):
        return ()


@dataclass(frozen=True)
class InterruptedAssignment(AssignmentDecision, AssignmentState):
    notification_changed = True
    decision: PendingDecision | Engagement
    preengagement_target = True

    @classmethod
    def build(cls, mode, execution_id, target):
        return cls(
            PendingDecision(mode) if execution_id is None else mode.engage(execution_id, target)
        )


class DeferredAssignment(SelectedResponseNotification, InterruptedAssignment):
    def requires_selected_triage(self) -> bool:
        if self.execution_id is not None:
            return False
        return super().requires_selected_triage()

    notification_state = InterruptedProgress

    deferred = True
    engageable = True

    @classmethod
    def successors(cls):
        return (
            TriagePendingAssignment,
            FullPendingAssignment,
            EngagedAssignment,
            DeliveredAssignment,
            FailedAssignment,
        )

    def permits_engagement_change(self, after):
        return super().permits_engagement_change(after) and not (
            self.execution_id is not None and (after.triage_pending or after.full_pending)
        )


@dataclass(frozen=True)
class DeliveredAssignment(AssignmentDecision, AssignmentState):
    """The recipient's running turn took this message as one of its inputs.

    No execution answers it: the agent replies with its own tools. Only the
    proved start of that input in the running turn reaches this state.
    """

    decision: PendingDecision
    notification_changed = True
    records_conversation_response = True

    def recorded_response(self, original, replies, recipient_lookup):
        return next(
            (
                reply.message
                for reply in replies
                if reply.audience.sender_lookup == recipient_lookup
            ),
            None,
        )

    def notification(
        self,
        recipient,
        sender_lookup,
        *,
        observation,
        activity,
        updated_at_ms=0,
        triage_inflight=False,
    ):
        notification = super().notification(
            recipient, sender_lookup, observation=observation, activity=activity
        )
        return activity.delivered_notification(notification, updated_at_ms)

    notification_state = IgnoredProgress

    terminal = True
    preengagement_target = True

    @classmethod
    def binding_expression(cls) -> str:
        return AssignmentState.binding_expression()

    @classmethod
    def build(cls, mode, execution_id, target):
        return cls(PendingDecision(mode))

    @classmethod
    def successors(cls):
        return ()


class FailedAssignment(InterruptedAssignment):
    def notification(
        self,
        recipient,
        sender_lookup,
        *,
        observation,
        activity,
        updated_at_ms=0,
        triage_inflight=False,
    ):
        notification = super().notification(
            recipient, sender_lookup, observation=observation, activity=activity
        )
        return replace(notification, state=FailedProgress(notification.state))

    def rejected_triage_history(self, execution, **source):
        """A proved rejected result is delivered source, never an IGNORE decision."""
        from .historical_native_inputs import FailedTriageHistoricalNativeInput

        return FailedTriageHistoricalNativeInput(execution=execution, **source)

    notification_state = FailedProgress

    terminal = True
    failed = True

    @classmethod
    def successors(cls):
        return ()
