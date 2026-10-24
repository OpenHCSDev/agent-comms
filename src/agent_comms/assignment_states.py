"""Claim decisions carry only the data valid before or after engagement."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar

from .coordination_errors import IdentityConflict, IntegrityViolationError
from .coordinator_vocabulary import CoordinatorVocabulary
from .field_codec import FieldCodec, ThreadLookup
from .lifecycle import LifecycleState
from .lifecycle_capability import Active, Complete, Deferred, Failed, Pending, Terminal
from .notification_progress import (
    CompletedProgress,
    ContextualProgress,
    EngagedProgress,
    FailedProgress,
    IgnoredProgress,
    InterruptedProgress,
    PassiveProgress,
    UnrecordedProgress,
)
from .source_proof_requirement import ProvedByInput, SettledWithoutInput, SourceSettlement, Unproved
from .typed_table import sql_literal
from .wake_policy import BoundedTriageWake, Engagement, FullWake, PassiveWake, WakePolicy

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from typing import Self

    from .agent_activity import RecipientActivity
    from .audience_manifest import FrozenRecipient
    from .bus_publication import Delivered
    from .coordination_tables.assignments import WakeAssignment
    from .coordination_tables.responses import ResponseObligation
    from .historical_native_inputs import FailedTriageHistoricalNativeInput, HistoricalNativeInput
    from .native_input_record import TriageNativeExecution
    from .notification_assignment import AssignmentActivity
    from .presentation import MessageNotification


@dataclass(frozen=True)
class PendingDecision:
    mode: WakePolicy

    def __post_init__(self):
        if not self.mode.items_with(Active, self.mode):
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
class AssignmentState(CoordinatorVocabulary, LifecycleState, affix="Assignment"):
    def source_settlement(
        self, evidence: tuple[HistoricalNativeInput, ...], policy: WakePolicy, claim_id: str,
    ) -> SourceSettlement:
        if not policy.items_with(Active, policy):
            return SettledWithoutInput()
        if policy.proves_source(evidence) and all(
            proof.assignment_id == claim_id for proof in evidence
        ):
            return ProvedByInput(evidence)
        return Unproved()

    @classmethod
    def sql_vocabulary(cls) -> dict[str, str]:
        members = cls.members_with(cls)

        def case(subject: str, arms: Iterable[tuple[type[AssignmentState], str]]) -> str:
            return (
                f"CASE {subject} "
                + " ".join(
                    f"WHEN {sql_literal(member.declared_name)} THEN {arm}" for member, arm in arms
                )
                + " END"
            )

        vocabulary = dict(
            assignment_names=cls.sql_values(cls.names()),
            assignment_binding=case(
                "disposition", ((member, f"({member.binding_expression()})") for member in members)
            ),
        )
        for suffix, expression in (
            ("", "lifecycle"),
            ("_new", "NEW.lifecycle"),
            ("_old", "OLD.lifecycle"),
        ):
            kind = f"json_extract({expression}, '$.kind')"
            vocabulary["assignment_mode" + suffix] = case(
                kind, ((member, member.mode_expression(expression)) for member in members)
            )
            vocabulary["assignment_verdict" + suffix] = case(
                kind, ((member, member.verdict_expression(expression)) for member in members)
            )
        return vocabulary

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

    def rejected_triage_history(self, execution: TriageNativeExecution, **source) -> FailedTriageHistoricalNativeInput | None:
        raise ValueError("Recorded triage lacks a decision or a terminal failed claim")

    def wake_frame(self, source: Delivered, obligation: ResponseObligation | None) -> tuple[str, str]:
        raise IdentityConflict("full wake frame requires the current response obligation")

    def requires_selected_triage(self) -> bool:
        if self.mode.triage:
            raise IdentityConflict("pending claim wake decision is not executable")
        return False

    def require_engagement(self) -> Engagement:
        raise IdentityConflict("wake assignment is not engaged")

    def require_engageable(self) -> None:
        raise IdentityConflict("claim cannot engage this execution")

    def require_completion(self) -> Engagement:
        raise IdentityConflict("wake assignment is not completed")

    notification_state: ClassVar[type[ContextualProgress]] = ContextualProgress
    records_conversation_response: ClassVar[bool] = False

    def recorded_response(
        self, original: Delivered, replies: Sequence[Delivered], recipient_lookup: ThreadLookup
    ) -> Delivered | None:
        return None

    def notification(
        self,
        recipient: FrozenRecipient,
        sender_lookup: ThreadLookup,
        *,
        observation: RecipientActivity,
        activity: AssignmentActivity | None = None,
        updated_at_ms: int = 0,
        triage_inflight: bool = False,
    ) -> MessageNotification:
        from .presentation import MessageNotification

        return MessageNotification(
            recipient,
            sender_lookup,
            self.notification_state(UnrecordedProgress() if activity is None else activity.progress),
        )


    @classmethod
    def mode_expression(cls, expression: str) -> str:
        return sql_literal(type(cls().mode))

    @classmethod
    def verdict_expression(cls, expression: str) -> str:
        return sql_literal(cls().verdict)

    @classmethod
    def binding_expression(cls) -> str:
        return "execution_id IS NULL AND exact_target IS NULL"

    def writable_pending_assignments(self, assignment: WakeAssignment) -> tuple[WakeAssignment, ...]:
        return ()

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
    def build(cls, mode: WakePolicy, execution_id: str | None, target: str | None) -> Self:
        return cls()

    @classmethod
    def load(
        cls, mode: WakePolicy, verdict: str | None, execution_id: str | None, target: str | None,
    ) -> Self:
        state = cls.build(mode, execution_id, target)
        if (state.mode, state.verdict, state.execution_id) != (mode, verdict, execution_id):
            raise IntegrityViolationError("claim decision relation is inconsistent")
        if state.exact_target != target:
            raise IntegrityViolationError("claim decision target is inconsistent")
        return state

    @classmethod
    def build_preengagement(cls, mode: WakePolicy) -> AssignmentState:
        raise IdentityConflict("preengagement transition is not declared")

    def harness_handoff(self) -> tuple[type[AssignmentState], ...]:
        """Steps when a harness outside Core takes the message; a settled claim keeps its state."""
        return ()

    def harness_delivery(self) -> tuple[type[AssignmentState], ...]:
        """Steps when that harness confirms the message started; a settled claim keeps its state."""
        return ()

    def preengagement(self, disposition: type[AssignmentState]) -> AssignmentState:
        if self.execution_id is not None or disposition not in self.successors():
            raise IdentityConflict("preengagement transition is not declared")
        after = disposition.build_preengagement(self.mode)
        if after.mode != self.mode:
            raise IdentityConflict("preengagement cannot change frozen wake policy")
        return after


class PassiveAssignment(Terminal, AssignmentState):
    notification_state = PassiveProgress

    @property
    def mode(self):
        return PassiveWake()


class EngageableAssignment:
    def require_engageable(self) -> None:
        pass


class PendingNotification(EngageableAssignment, Pending):
    """Pending relevance/full execution membership, not another display state."""

    def harness_handoff(self) -> tuple[type[AssignmentState], ...]:
        # Unbound DEFERRED is UNKNOWN input: never selected, so never replayed natively.
        return (DeferredAssignment,)

    def harness_delivery(self) -> tuple[type[AssignmentState], ...]:
        return (DeferredAssignment, DeliveredAssignment)

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
        recipient: FrozenRecipient,
        sender_lookup: ThreadLookup,
        *,
        observation: RecipientActivity,
        activity: AssignmentActivity | None = None,
        updated_at_ms: int = 0,
        triage_inflight: bool = False,
    ) -> MessageNotification:
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
        recipient: FrozenRecipient,
        sender_lookup: ThreadLookup,
        *,
        observation: RecipientActivity,
        activity: AssignmentActivity,
        updated_at_ms: int = 0,
        triage_inflight: bool = False,
    ) -> MessageNotification:
        notification = super().notification(
            recipient, sender_lookup, observation=observation, activity=activity
        )
        return observation.selected_response(
            notification, updated_at_ms, activity.progress
        )


class PreengagementTarget:
    @classmethod
    def build_preengagement(cls, mode: WakePolicy) -> AssignmentState:
        return cls.build(mode, None, None)


class ChangedNotification:
    def notification(
        self, recipient: FrozenRecipient, sender_lookup: ThreadLookup, **kwargs
    ) -> MessageNotification:
        return replace(
            super().notification(recipient, sender_lookup, **kwargs), changed=True
        )


class TriagePendingAssignment(PreengagementTarget, PendingNotification, AssignmentState):
    def wake_frame(self, source: Delivered, obligation: ResponseObligation | None) -> tuple[str, str]:
        if obligation is not None:
            raise IdentityConflict("bounded triage cannot inherit a response obligation")
        return self.mode.triage_expectation(), "No response obligation exists until triage engages."


    def requires_selected_triage(self) -> bool:
        return True

    @property
    def mode(self):
        return BoundedTriageWake()

    @classmethod
    def successors(cls):
        return IgnoredAssignment, EngagedAssignment, DeferredAssignment, FailedAssignment


class FullPendingAssignment(PreengagementTarget, PendingNotification, AssignmentState):
    def writable_pending_assignments(self, assignment: WakeAssignment) -> tuple[WakeAssignment, ...]:
        return (assignment,)

    @property
    def mode(self):
        return FullWake()

    @classmethod
    def successors(cls):
        return EngagedAssignment, DeferredAssignment, FailedAssignment


class IgnoredAssignment(ChangedNotification, PreengagementTarget, Terminal, AssignmentState):
    notification_state = IgnoredProgress

    @property
    def mode(self):
        return BoundedTriageWake()

    @property
    def verdict(self):
        return "ignore"


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
            for member in WakePolicy.members_with(Active)
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
class BoundAssignment(ChangedNotification, AssignmentDecision, AssignmentState):
    decision: Engagement

    @classmethod
    def binding_expression(cls) -> str:
        return "execution_id IS NOT NULL AND exact_target IS NOT NULL"

    @classmethod
    def build(cls, mode: WakePolicy, execution_id: str | None, target: str | None) -> Self:
        return cls(mode.engage(execution_id, target))


class EngagedAssignment(SelectedResponseNotification, BoundAssignment):
    notification_state = EngagedProgress
    def wake_frame(self, source: Delivered, obligation: ResponseObligation | None) -> tuple[str, str]:
        if obligation is None:
            raise IdentityConflict("full wake frame requires the current response obligation")
        obligation.lifecycle.require_preparation()
        if (obligation.execution_id, obligation.exact_target) != (
            self.decision.execution_id,
            source.exact_reply_target,
        ):
            raise IdentityConflict("full wake frame requires the current response obligation")
        return self.mode.full_expectation(), "you owe a response: " + FieldCodec.compact(
            {
                "target": obligation.exact_target,
                "source_seq": source.seq,
                "execution_id": obligation.execution_id,
            }, ensure_ascii=True,
        )

    def require_engagement(self) -> Engagement:
        return self.decision


    @classmethod
    def successors(cls):
        return CompletedAssignment, DeferredAssignment, FailedAssignment


class CompletedAssignment(Complete, Terminal, BoundAssignment):
    def notification(
        self,
        recipient: FrozenRecipient,
        sender_lookup: ThreadLookup,
        *,
        observation: RecipientActivity,
        activity: AssignmentActivity,
        updated_at_ms: int = 0,
        triage_inflight: bool = False,
    ) -> MessageNotification:
        notification = super().notification(
            recipient, sender_lookup, observation=observation, activity=activity
        )
        return activity.completed_notification(notification)

    def require_completion(self) -> Engagement:
        return self.decision

    notification_state = CompletedProgress


@dataclass(frozen=True)
class InterruptedAssignment(
    ChangedNotification, PreengagementTarget, AssignmentDecision, AssignmentState
):
    decision: PendingDecision | Engagement

    @classmethod
    def build(cls, mode: WakePolicy, execution_id: str | None, target: str | None) -> Self:
        return cls(
            PendingDecision(mode) if execution_id is None else mode.engage(execution_id, target)
        )


class DeferredAssignment(
    EngageableAssignment, Deferred, SelectedResponseNotification, InterruptedAssignment
):
    def requires_selected_triage(self) -> bool:
        if self.execution_id is not None:
            return False
        return super().requires_selected_triage()

    notification_state = InterruptedProgress

    def harness_delivery(self) -> tuple[type[AssignmentState], ...]:
        return (DeliveredAssignment,)

    @classmethod
    def successors(cls):
        return (
            TriagePendingAssignment,
            FullPendingAssignment,
            EngagedAssignment,
            DeliveredAssignment,
            FailedAssignment,
        )


@dataclass(frozen=True)
class DeliveredAssignment(
    ChangedNotification, PreengagementTarget, Terminal, AssignmentDecision, AssignmentState
):
    """The recipient's running turn took this message as one of its inputs.

    No execution answers it: the agent replies with its own tools. Only the
    proved start of that input reaches this state: the native turn's own
    record, or the confirmed outcome of the external harness that took it.
    """

    decision: PendingDecision
    records_conversation_response = True

    def recorded_response(
        self, original: Delivered, replies: Sequence[Delivered], recipient_lookup: ThreadLookup
    ) -> Delivered | None:
        return next(
            (
                reply.message
                for reply in replies
                if reply.sender_lookup == recipient_lookup
            ),
            None,
        )

    def notification(
        self,
        recipient: FrozenRecipient,
        sender_lookup: ThreadLookup,
        *,
        observation: RecipientActivity,
        activity: AssignmentActivity,
        updated_at_ms: int = 0,
        triage_inflight: bool = False,
    ) -> MessageNotification:
        notification = super().notification(
            recipient, sender_lookup, observation=observation, activity=activity
        )
        return activity.delivered_notification(notification, updated_at_ms)

    notification_state = IgnoredProgress

    @classmethod
    def binding_expression(cls) -> str:
        return AssignmentState.binding_expression()

    @classmethod
    def build(cls, mode: WakePolicy, execution_id: str | None, target: str | None) -> Self:
        return cls(PendingDecision(mode))


class FailedAssignment(Failed, Terminal, InterruptedAssignment):
    def source_settlement(
        self, evidence: tuple[HistoricalNativeInput, ...], policy: WakePolicy, claim_id: str,
    ) -> SourceSettlement:
        return (super().source_settlement(evidence, policy, claim_id)
                if evidence else SettledWithoutInput())

    def notification(
        self,
        recipient: FrozenRecipient,
        sender_lookup: ThreadLookup,
        *,
        observation: RecipientActivity,
        activity: AssignmentActivity | None,
        updated_at_ms: int = 0,
        triage_inflight: bool = False,
    ) -> MessageNotification:
        notification = super().notification(
            recipient, sender_lookup, observation=observation, activity=activity
        )
        return replace(notification, state=FailedProgress(notification.state))

    def rejected_triage_history(self, execution: TriageNativeExecution, **source) -> FailedTriageHistoricalNativeInput | None:
        """A proved rejected result is delivered source, never an IGNORE decision."""
        from .historical_native_inputs import FailedTriageHistoricalNativeInput

        return FailedTriageHistoricalNativeInput(execution=execution, **source)

    notification_state = FailedProgress
