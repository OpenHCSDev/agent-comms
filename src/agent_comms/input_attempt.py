"""Durable input observations, never an execution or replay capability."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field, fields, replace
from typing import Annotated, ClassVar, Self

from agent_comms.errors import RelationViolationError
from agent_comms.execution.context_provenance_source import ContextProvenanceSource
from agent_comms.execution.input_key import InputKey, InputKeyText, PublicInputKeyText
import agent_comms.execution.input_origin as input_origin
from agent_comms.execution.native_input_proof import NativeContextProof
from agent_comms.execution.reported_turn import ReportedTurnState
from agent_comms.messaging.notification_evidence import ConsumptionEvidence
from agent_comms.messaging.notification_progress import DeliveryProgress, ShownToModelProgress
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import (
    FieldCodec,
    GoalId,
    InputId,
    NullMember,
    PublicExclude,
    RootId,
    TurnId,
    WireName,
    WireOmitDefault,
    WireRequired,
    boundary_decoder,
)
from agent_comms.nominal.lifecycle_capability import LifecycleCapability, Unresolved
from agent_comms.nominal.text_digest import TextDigest
from agent_comms.registry.registry_names import RegistryNames
from agent_comms.registry.thread_identity import GenerationCounter, ThreadIncarnation, TurnFence
from agent_comms.registry.thread_identity_source import ThreadIdentitySource


class InputAlreadyCleared(RelationViolationError):
    """A positively cleared input cannot acquire another native write."""


class KnownInputState:
    """A reported definite disposition, not permission to write or replay input."""

    @classmethod
    def failure_result[T](cls, known: Callable[[], T], unknown: Callable[[], T]) -> T:
        return known()


class ClearedInputState(KnownInputState):
    public_status = "cleared"
    cancellation_feedback = "Input cleared before native start."

    def require_not_cleared(self) -> None:
        raise InputAlreadyCleared("Input was explicitly cleared")


@dataclass(frozen=True, slots=True)
class GoalInputDecision:
    goal_revision: int
    turn_id: ReportedTurnState


class InputTransition:
    """What one transition did to an input: moved it to a named state, or left it where it was."""

    @abstractmethod
    def successors(self, key: InputKey) -> dict[InputKey, StoredInput]:
        """The rows this transition writes under key."""

    @abstractmethod
    def require_moved(self, refusal: str) -> MovedInput: ...


@dataclass(frozen=True)
class MovedInput(InputTransition):
    state: StoredInput

    def successors(self, key: InputKey) -> dict[InputKey, StoredInput]:
        return {key: self.state}

    def require_moved(self, refusal: str) -> MovedInput:
        return self


@dataclass(frozen=True)
class UnchangedInput(InputTransition):
    state: InputAttempt

    def successors(self, key: InputKey) -> dict[InputKey, StoredInput]:
        return {}

    def require_moved(self, refusal: str) -> MovedInput:
        raise RelationViolationError(refusal)


@dataclass(frozen=True)
class InputAttempt(LifecycleCapability, ConsumptionEvidence, DeclaredFamily, affix="Input"):
    exists: ClassVar[bool] = False
    accepts_reservation: ClassVar[bool] = False
    has_started: ClassVar[bool] = False
    has_native_binding: ClassVar[bool] = False
    cancellation_feedback: ClassVar[str] = (
        "Delivery unconfirmed. Inspect delivery before deciding whether to send a new message."
    )

    @property
    @abstractmethod
    def public_status(self) -> str: ...

    @classmethod
    @boundary_decoder
    def reported(cls, state: type[InputAttempt] | None) -> type[InputAttempt]:
        """Interpret an optional wire report without inventing a stored input."""
        return UnreportedInput if state is None else state

    @classmethod
    def failure_result[T](cls, known: Callable[[], T], unknown: Callable[[], T]) -> T:
        return unknown()

    def source_text_or(self, fallback: str) -> str:
        return fallback

    def matches_owner(self, source_owner: ThreadIncarnation) -> bool:
        return False

    def require_original_provenance(self, source: input_origin.InputProvenance):
        raise RelationViolationError("Constraint lacks its original input provenance")

    def matches_claim(self, claim_scope: int) -> bool:
        return False

    def unresolved_claim(self, claim_scope: int) -> bool:
        return any(row.matches_claim(claim_scope) for row in self.items_with(Unresolved, self))

    def queued_for(self, owner: ThreadIncarnation, claim_scope: int, text: str) -> bool:
        return False

    def queued_text(self, owner: ThreadIncarnation, claim_scope: int) -> str:
        """The text of input still reserved for this owner claim; nothing else is queued."""
        raise RelationViolationError("Startup input no longer belongs to this owner claim_scope")

    def require_started(self, claim_scope: int) -> StartedInput:
        raise RelationViolationError("Input start lacks its original native disposition")

    def context_provenance(self) -> input_origin.InputProvenance:
        raise RelationViolationError("Constraint lacks its original input")

    def model_contexts(self) -> tuple[NativeContextProof, ...]:
        return ()

    def consumed_progress(self, progress: DeliveryProgress) -> DeliveryProgress:
        return progress

    def context_committed(
        self,
        lease: TurnFence,
        sent_text: str,
        context: NativeContextProof,
        *,
        snapshot: RegistryNames[ThreadIdentitySource],
    ) -> InputTransition:
        raise RelationViolationError("Context acknowledgement lacks its started input")

    def bind(
        self, *, claim_scope: int, turn_id: TurnId, native_id: InputId, text: str
    ) -> InputTransition:
        self.require_not_cleared()
        return UnchangedInput(self)

    def started(self, *, turn_id: TurnId, native_id: InputId, text: str) -> InputTransition:
        return UnchangedInput(self)

    def finish_unbound(self) -> InputTransition:
        return UnchangedInput(self)

    def clear_unbound(self) -> InputTransition:
        return UnchangedInput(self)

    def clear_native(self, native_id: InputId, text: str) -> InputTransition:
        return UnchangedInput(self)

    def require_not_cleared(self) -> None:
        """Non-cleared states retain their existing write checks."""

    def matches_native(self, *, turn_id: TurnId, native_id: InputId, text: str) -> bool:
        return False

    def started_for_native(
        self,
        lease: TurnFence,
        native_id: InputId,
        sent_text: str,
        *,
        snapshot: RegistryNames[ThreadIdentitySource],
    ) -> bool:
        return False

    def unbound_delivery(self, root_id: RootId) -> input_origin.InputDelivery:
        """Explicit withdrawal never grants an automatic delivery retry."""
        return input_origin.UndeliveredInput()

    def delivered_in(
        self, lease: TurnFence, root_id: RootId, snapshot: RegistryNames[ThreadIdentitySource]
    ) -> input_origin.InputDelivery:
        """The bus message this input delivered into the lease's turn."""
        return input_origin.UndeliveredInput()


class InputDeliveryScope(DeclaredFamily, affix="InputDeliveryScope"):
    """The queue evidence available to this delivery read, not an input outcome."""

    pending_label: ClassVar[str] = "not confirmed"
    notices_label: ClassVar[str] = "earlier notices"
    """Plain words, because these are counted into a status line a person reads: they were
    "unconfirmed" and "historical notices", which name the read's own bookkeeping rather
    than telling the reader what happened to their input (rules/talk.md)."""

    @abstractmethod
    def historical_notice(self, row: StoredInput) -> bool:
        raise NotImplementedError


@dataclass(frozen=True)
class UnobservedInputDeliveryScope(NullMember, InputDeliveryScope, declared_name="unobserved"):
    def historical_notice(self, row: StoredInput) -> bool:
        return row.notice_dismissed


@dataclass(frozen=True)
class OwnerQueueInputDeliveryScope(InputDeliveryScope, declared_name="owner_queue"):
    keys: frozenset[Annotated[InputKey, InputKeyText]]
    pending_label: ClassVar[str] = "waiting to start"

    def historical_notice(self, row: StoredInput) -> bool:
        return row.key not in self.keys


UNOBSERVED_DELIVERY_SCOPE = UnobservedInputDeliveryScope()


@dataclass(frozen=True)
class UnresolvedInputReference:
    """An unresolved input named by its identity, so a page listing many of them never carries
    a second copy of each body. Its key says where the body is: a bus input's key is its bus
    sequence; a direct input has no bus row, and comms_input_delivery shows its text."""

    target: str
    input_id: Annotated[InputKey, PublicInputKeyText, WireName("inputId")]
    status: str
    reviewed_for_goals: Annotated[
        tuple[str, ...], WireName("reviewedForGoals"), WireOmitDefault()
    ] = ()

    @classmethod
    def of(cls, row: StoredInput, **declared: object) -> Self:
        """Every member is built from a stored row the same way; a member adds only its own
        fields, through row_fields or the caller's declared ones."""
        return cls(target=row.target, input_id=row.key,
                   status=row.public_status, reviewed_for_goals=tuple(sorted(row.goal_reviews)),
                   **cls.row_fields(row), **declared)

    @classmethod
    def row_fields(cls, row: StoredInput) -> dict[str, object]:
        return {}


@dataclass(frozen=True)
class UnresolvedInput(UnresolvedInputReference):
    """An unresolved input shown to a person, with its text; its owner, claim and origin stay
    in the store."""

    text: str = field(kw_only=True)

    @classmethod
    def row_fields(cls, row: StoredInput) -> dict[str, object]:
        return {"text": row.source_text}

    def record_text(self) -> str:
        return (f"Input: {self.input_id.public_text()} · Target: {self.target}\n"
                f"{self.text}")


@dataclass(frozen=True)
class HistoricalInput(UnresolvedInput):
    """An earlier notice of an unresolved input, shown with whether the owner dismissed it."""

    notice_dismissed: Annotated[bool, WireName("noticeDismissed")] = field(
        kw_only=True
    )

    def record_text(self) -> str:
        return super().record_text() + ("\nNotice cleared." if self.notice_dismissed else "")

    @classmethod
    def notice(cls, row: StoredInput, notice_dismissed: bool) -> HistoricalInput:
        return cls.of(row, notice_dismissed=notice_dismissed)


@dataclass(frozen=True)
class StoredInput(InputAttempt, ContextProvenanceSource[input_origin.InputProvenance]):
    """The recorded owner name and input generation, without inventing a thread identity."""

    exists = True

    def source_text_or(self, fallback: str) -> str:
        return self.source_text

    def context_provenance(self) -> input_origin.InputProvenance:
        return input_origin.InputProvenance(self.key, self.origin)

    def require_original_provenance(self, source: input_origin.InputProvenance):
        if self.context_provenance() != source:
            raise RelationViolationError("Constraint lacks its original input provenance")
        self.origin.require_human()
        return self

    key: Annotated[InputKey, InputKeyText]
    sequence: int | None
    owner: str
    claim_generation: int
    target: str
    source_text: str
    origin: Annotated[input_origin.InputOrigin, WireOmitDefault()] = field(
        default_factory=input_origin.UnattributedInputOrigin, kw_only=True
    )
    notice_dismissed: Annotated[bool, WireOmitDefault()] = field(
        default=False, kw_only=True
    )
    goal_reviews: Annotated[
        dict[str, GoalInputDecision], WireOmitDefault()
    ] = field(default_factory=dict, kw_only=True)

    def __post_init__(self) -> None:
        try:
            for value in (self.owner, self.target, self.source_text):
                if not FieldCodec.decode(str, value):
                    raise ValueError("Input identity text cannot be empty")
            GenerationCounter.require_positive(self.claim_generation)
        except ValueError as error:
            raise ValueError("Invalid ACP input identity") from error
        self.key.require_sequence(self.sequence)

    @property
    def digest(self) -> TextDigest:
        return TextDigest.of(self.source_text)

    def matches_owner(self, source_owner: ThreadIncarnation) -> bool:
        # The origin owns author evidence; an owned bus key additionally
        # retains its original recipient birth. Neither invents a missing birth.
        return (self.origin.matches_owner(self.owner, source_owner)
                and source_owner.recorded_or(
                    lambda recorded: self.key.matches_recorded_owner(recorded.created_at),
                    self.key.matches_unrecorded_owner,
                ))

    def matches_claim(self, claim_scope: int) -> bool:
        return self.claim_generation == claim_scope

    def _transition(self, target: type[StoredInput], **changes) -> StoredInput:
        values = {item.name: getattr(self, item.name) for item in fields(self)}
        return target(**values, **changes)

    @property
    def order(self) -> tuple[bool, int]:
        return self.sequence is None, self.sequence or 0

    def bus_sequences_through(self, through: int) -> tuple[int, ...]:
        """Return this input's bus sequence only if it is within the requested limit."""
        return (self.sequence,) if self.sequence is not None and self.sequence <= through else ()

    def reviewed_for_goal(self, goal_id: GoalId) -> bool:
        return goal_id in self.goal_reviews

    def review(self, goal_id: GoalId, decision: GoalInputDecision) -> StoredInput:
        self.require(Unresolved, ValueError("Reviewed inputs changed; inspect them again."))
        return replace(self, goal_reviews={**self.goal_reviews, goal_id: decision})


class RecoverableUnboundInput(StoredInput):
    def unbound_delivery(self, root_id: RootId) -> input_origin.InputDelivery:
        return self.origin.delivery_reference(root_id)


class ReservedInput(Unresolved, RecoverableUnboundInput):
    public_status = "unknown"

    accepts_reservation = True

    def queued_for(self, owner: ThreadIncarnation, claim_scope: int, text: str) -> bool:
        return (
            self.matches_owner(owner)
            and self.matches_claim(claim_scope)
            and self.digest.matches(text)
        )

    def queued_text(self, owner: ThreadIncarnation, claim_scope: int) -> str:
        if not (self.matches_owner(owner) and self.matches_claim(claim_scope)):
            return super().queued_text(owner, claim_scope)
        return self.source_text

    def bind(
        self, *, claim_scope: int, turn_id: TurnId, native_id: InputId, text: str
    ) -> InputTransition:
        if self.claim_generation != claim_scope:
            return UnchangedInput(self)
        return MovedInput(self._transition(
            BoundUnknownInput, turn_id=turn_id, native_id=native_id, sent_digest=TextDigest.of(text)
        ))

    def finish_unbound(self) -> InputTransition:
        return MovedInput(self._transition(NotSentInput))

    def clear_unbound(self) -> InputTransition:
        return MovedInput(self._transition(ClearedUnsentInput))


@dataclass(frozen=True)
class SentInput(StoredInput):
    has_native_binding = True
    turn_id: Annotated[TurnId, PublicExclude(), WireRequired()]
    native_id: Annotated[
        InputId, PublicExclude(), WireRequired()
    ]
    # The native session owns the sent text; this row only proves which text it was.
    sent_digest: Annotated[
        TextDigest, PublicExclude(), WireRequired()
    ]

    def __post_init__(self) -> None:
        super().__post_init__()

    def matches_native(self, *, turn_id: TurnId, native_id: InputId, text: str) -> bool:
        return (self.turn_id, self.native_id) == (turn_id, native_id) and self.sent_digest.matches(text)


class BoundUnknownInput(Unresolved, SentInput):
    public_status = "unknown"

    def clear_native(self, native_id: InputId, text: str) -> InputTransition:
        if self.native_id == native_id and self.sent_digest.matches(text):
            return MovedInput(self._transition(ClearedNativeInput))
        return UnchangedInput(self)

    def started(self, *, turn_id: TurnId, native_id: InputId, text: str) -> InputTransition:
        if self.matches_native(turn_id=turn_id, native_id=native_id, text=text):
            return MovedInput(self._transition(StartedInput))
        return UnchangedInput(self)


class ClearedUnsentInput(ClearedInputState, StoredInput):
    """Explicit withdrawal before any native binding; never a retry grant."""


class ClearedNativeInput(ClearedInputState, SentInput):
    """The native queue confirmed removal; original wire binding stays intact."""


class StartedInput(KnownInputState, SentInput):
    has_started = True
    public_status = "started"
    cancellation_feedback = "Native input started; turn cancelled."

    def require_started(self, claim_scope: int) -> StartedInput:
        if not self.matches_claim(claim_scope):
            raise RelationViolationError("Input start claim_scope changed")
        return self

    def matches_lease(self, lease, snapshot) -> bool:
        return (
            lease.identity.incarnation.matches_recorded_name(self.owner, snapshot)
            and self.matches_claim(lease.claimed_generation)
            and self.turn_id == lease.turn_id
        )

    def delivered_in(self, lease, root_id, snapshot) -> input_origin.InputDelivery:
        if self.matches_lease(lease, snapshot):
            return self.origin.delivery_reference(root_id)
        return input_origin.UndeliveredInput()

    def started_for_native(
        self,
        lease: TurnFence,
        native_id: InputId,
        sent_text: str,
        *,
        snapshot: RegistryNames[ThreadIdentitySource],
    ) -> bool:
        """Whether this original row's recorded lease/input evidence is this native start.

        Stored rows record owner name and input generation, not thread birth or process. The
        publication source independently validates the original complete lease
        and native ancestry; this acknowledgement never invents their missing proof.
        """
        return self.matches_lease(lease, snapshot) and self.matches_native(
            turn_id=lease.turn_id, native_id=native_id, text=sent_text)

    def context_committed(
        self,
        lease: TurnFence,
        sent_text: str,
        context: NativeContextProof,
        *,
        snapshot: RegistryNames[ThreadIdentitySource],
    ) -> InputTransition:
        if not self.started_for_native(lease, context.input_id, sent_text, snapshot=snapshot):
            raise RelationViolationError(
                "Context acknowledgement differs from its original input binding"
            )
        return self.record_context(context)

    def record_context(self, context: NativeContextProof) -> InputTransition:
        return MovedInput(self._transition(ContextConsumedInput, context=context))


@dataclass(frozen=True)
class ContextConsumedInput(StartedInput):
    """The original started binding plus its first independently corroborated context."""

    context: Annotated[NativeContextProof, PublicExclude(), WireRequired()]

    def model_contexts(self) -> tuple[NativeContextProof, ...]:
        return (self.context,)

    def consumed_progress(self, progress: DeliveryProgress) -> DeliveryProgress:
        return progress.model_consumed(ShownToModelProgress(self.context.committed_at_ms))

    def record_context(self, context: NativeContextProof) -> InputTransition:
        self.context.require_continuation(context)
        return UnchangedInput(self)


class NotSentInput(KnownInputState, Unresolved, RecoverableUnboundInput):
    public_status = "not_sent"
    cancellation_feedback = (
        "Not sent — cancellation completed before native delivery."
    )


class MissingInput(InputAttempt):
    public_status = "missing"


class UnreportedInput(InputAttempt):
    """The failure reported no input disposition; the wire's absence stays absent."""

    public_status = "unconfirmed"
