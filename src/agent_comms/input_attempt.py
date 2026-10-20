"""Durable input observations, never an execution or replay capability."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field, fields, replace
from typing import TYPE_CHECKING, ClassVar

from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import FieldCodec, Id128Text, NullMember
from .input_origin import (
    InputDelivery,
    InputOrigin,
    InputProvenance,
    UnattributedInputOrigin,
    UndeliveredInput,
)
from .text_digest import TextDigest
from .thread_identity import GenerationCounter, ThreadIncarnation

if TYPE_CHECKING:
    from .registry_document import RegistrySnapshot
    from .turn_lease import TurnLeaseFence



class InputAlreadyCleared(RelationViolationError):
    """A positively cleared input cannot acquire another native write."""


class ClearedInputState:
    public_status = "cleared"
    cancellation_feedback = "Input cleared before native start."

    def require_not_cleared(self) -> None:
        raise InputAlreadyCleared("Input was explicitly cleared")


@dataclass(frozen=True, slots=True)
class GoalInputDecision:
    goal_revision: int
    turn_id: str


class InputTransition:
    """What one transition did to an input: moved it to a named state, or left it where it was."""

    @abstractmethod
    def successors(self, key: str) -> dict[str, StoredInput]:
        """The rows this transition writes under key."""

    @abstractmethod
    def require_moved(self, refusal: str) -> MovedInput: ...


@dataclass(frozen=True)
class MovedInput(InputTransition):
    state: StoredInput

    def successors(self, key: str) -> dict[str, StoredInput]:
        return {key: self.state}

    def require_moved(self, refusal: str) -> MovedInput:
        return self


@dataclass(frozen=True)
class UnchangedInput(InputTransition):
    state: InputAttempt

    def successors(self, key: str) -> dict[str, StoredInput]:
        return {}

    def require_moved(self, refusal: str) -> MovedInput:
        raise RelationViolationError(refusal)


@dataclass(frozen=True)
class InputAttempt(DeclaredFamily, affix="Input"):
    exists: ClassVar[bool] = False
    accepts_reservation: ClassVar[bool] = False
    has_started: ClassVar[bool] = False
    has_native_binding: ClassVar[bool] = False
    unresolved: ClassVar[bool] = False
    cancellation_feedback: ClassVar[str] = (
        "Delivery unconfirmed. Inspect delivery before deciding whether to send a new message."
    )

    @property
    @abstractmethod
    def public_status(self) -> str: ...

    def matches_owner(self, source_owner: ThreadIncarnation) -> bool:
        return False

    def require_original_provenance(self, source: InputProvenance):
        raise RelationViolationError("Constraint lacks its original input provenance")

    def matches_claim(self, claim_scope: int) -> bool:
        return False

    def queued_for(self, owner: ThreadIncarnation, claim_scope: int, text: str) -> bool:
        return False

    def require_started(self, claim_scope: int) -> StartedInput:
        raise RelationViolationError("Input start lacks its original native disposition")

    def context_provenance(self) -> InputProvenance:
        raise RelationViolationError("Constraint lacks its original input")

    def bind(
        self, *, claim_scope: int, turn_id: str, native_id: str, text: str
    ) -> InputTransition:
        self.require_not_cleared()
        return UnchangedInput(self)

    def started(self, *, turn_id: str, native_id: str, text: str) -> InputTransition:
        return UnchangedInput(self)

    def finish_unbound(self) -> InputTransition:
        return UnchangedInput(self)

    def clear_unbound(self) -> InputTransition:
        return UnchangedInput(self)

    def clear_native(self, native_id: str, text: str) -> InputTransition:
        return UnchangedInput(self)

    def require_not_cleared(self) -> None:
        """Non-cleared states retain their existing write checks."""

    def matches_native(self, *, turn_id: str, native_id: str, text: str) -> bool:
        return False

    def started_for_native(
        self,
        lease: TurnLeaseFence,
        native_id: str,
        sent_text: str,
        *,
        snapshot: RegistrySnapshot,
    ) -> bool:
        return False

    def unbound_delivery(self, root_id: str) -> InputDelivery:
        """Explicit withdrawal never grants an automatic delivery retry."""
        return UndeliveredInput()

    def delivered_in(
        self, lease: TurnLeaseFence, root_id: str, snapshot: RegistrySnapshot
    ) -> InputDelivery:
        """The bus message this input delivered into the lease's turn."""
        return UndeliveredInput()


class InputDeliveryScope(DeclaredFamily, affix="InputDeliveryScope"):
    """The queue evidence available to this delivery read, not an input outcome."""

    owner_queue: ClassVar[bool] = False

    @abstractmethod
    def historical_notice(self, row: StoredInput) -> bool:
        raise NotImplementedError


@dataclass(frozen=True)
class UnobservedInputDeliveryScope(NullMember, InputDeliveryScope, declared_name="unobserved"):
    def historical_notice(self, row: StoredInput) -> bool:
        return row.notice_dismissed


@dataclass(frozen=True)
class OwnerQueueInputDeliveryScope(InputDeliveryScope, declared_name="owner_queue"):
    keys: frozenset[str]
    owner_queue: ClassVar[bool] = True

    def historical_notice(self, row: StoredInput) -> bool:
        return row.key not in self.keys


UNOBSERVED_DELIVERY_SCOPE = UnobservedInputDeliveryScope()


@dataclass(frozen=True)
class UnresolvedInput:
    """An unresolved input as a reply shows it; its owner, claim and origin stay in the store."""

    sequence: int | None
    target: str
    text: str
    input_id: str = field(metadata={"wire_name": "inputId"})
    status: str
    reviewed_for_goals: tuple[str, ...] = field(
        default=(), metadata={"wire_name": "reviewedForGoals", "wire_omit_default": True})

    @classmethod
    def of(cls, row: StoredInput) -> UnresolvedInput:
        return UnresolvedInput(
            sequence=row.sequence, target=row.target, text=row.source_text,
            input_id=row.public_id, status=row.public_status,
            reviewed_for_goals=tuple(sorted(row.goal_reviews)))


@dataclass(frozen=True)
class HistoricalInput(UnresolvedInput):
    """An earlier notice of an unresolved input, shown with whether the owner dismissed it."""

    notice_dismissed: bool = field(kw_only=True, metadata={"wire_name": "noticeDismissed"})

    @classmethod
    def notice(cls, row: StoredInput, notice_dismissed: bool) -> HistoricalInput:
        return HistoricalInput(
            sequence=row.sequence, target=row.target, text=row.source_text,
            input_id=row.public_id, status=row.public_status,
            reviewed_for_goals=tuple(sorted(row.goal_reviews)), notice_dismissed=notice_dismissed)


@dataclass(frozen=True)
class StoredInput(InputAttempt):
    """The recorded owner name and input generation, without inventing a thread identity."""

    exists = True

    def context_provenance(self) -> InputProvenance:
        return InputProvenance(self.key, self.origin)

    def require_original_provenance(self, source: InputProvenance):
        if self.context_provenance() != source:
            raise RelationViolationError("Constraint lacks its original input provenance")
        self.origin.require_human()
        return self
    key: str
    sequence: int | None
    owner: str
    claim_scope: int = field(metadata={"wire_name": 'admission'})
    target: str
    source_text: str
    origin: InputOrigin = field(default_factory=UnattributedInputOrigin, kw_only=True,
                               metadata={"wire_omit_default": True})
    notice_dismissed: bool = field(default=False, metadata={"wire_omit_default": True},
                                   kw_only=True)
    goal_reviews: dict[str, GoalInputDecision] = field(
        default_factory=dict, metadata={"wire_omit_default": True}, kw_only=True)

    def __post_init__(self) -> None:
        try:
            for value in (self.key, self.owner, self.target, self.source_text):
                if not FieldCodec.decode(str, value):
                    raise ValueError("Input identity text cannot be empty")
            GenerationCounter.require_positive(self.claim_scope)
        except ValueError as error:
            raise ValueError("Invalid ACP input identity") from error
        if self.sequence is not None and (
            FieldCodec.decode(int, self.sequence) <= 0
            or (
                self.key != f"bus:{self.sequence}"
                and not self.key.startswith(f"bus:{self.sequence}:owner:")
            )
        ):
            raise ValueError("Bus input key and sequence disagree")

    @property
    def digest(self) -> TextDigest:
        return TextDigest.of(self.source_text)

    def matches_owner(self, source_owner: ThreadIncarnation) -> bool:
        # Input with a recorded author uses that author's creation time. Older
        # rows with only a name do not get an inferred thread identity.
        return self.origin.matches_owner(self.owner, source_owner)

    def matches_claim(self, claim_scope: int) -> bool:
        return self.claim_scope == claim_scope

    def _transition(self, target: type[StoredInput], **changes) -> StoredInput:
        values = {item.name: getattr(self, item.name) for item in fields(self)}
        return target(**values, **changes)

    @property
    def order(self) -> tuple[bool, int]:
        return self.sequence is None, self.sequence or 0

    def bus_sequences_through(self, through: int) -> tuple[int, ...]:
        """Return this input's bus sequence only if it is within the requested limit."""
        return (self.sequence,) if self.sequence is not None and self.sequence <= through else ()

    def reviewed_for_goal(self, goal_id: str) -> bool:
        return goal_id in self.goal_reviews

    def review(self, goal_id: str, decision: GoalInputDecision) -> StoredInput:
        if not self.unresolved:
            raise ValueError("Reviewed inputs changed; inspect them again.")
        return replace(self, goal_reviews={**self.goal_reviews, goal_id: decision})

    @property
    def public_id(self) -> str:
        return self.key.removeprefix("acp:")


class RecoverableUnboundInput(StoredInput):
    def unbound_delivery(self, root_id: str) -> InputDelivery:
        return self.origin.delivery_reference(root_id)


class ReservedInput(RecoverableUnboundInput):
    unresolved = True
    public_status = "unknown"

    accepts_reservation = True

    def queued_for(self, owner: ThreadIncarnation, claim_scope: int, text: str) -> bool:
        return (
            self.matches_owner(owner)
            and self.matches_claim(claim_scope)
            and self.digest.matches(text)
        )

    def bind(
        self, *, claim_scope: int, turn_id: str, native_id: str, text: str
    ) -> InputTransition:
        if self.claim_scope != claim_scope:
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
    turn_id: str = field(metadata={"public_exclude": True, "wire_required": True})
    native_id: str = field(metadata={"public_exclude": True, "wire_required": True})
    # The native session owns the sent text; this row only proves which text it was.
    sent_digest: TextDigest = field(metadata={"public_exclude": True, "wire_required": True})

    def __post_init__(self) -> None:
        super().__post_init__()
        try:
            from .thread_identity import TurnId

            TurnId.for_registration(self.turn_id)
            Id128Text.decode(self.native_id)
        except ValueError as error:
            raise ValueError("Invalid native input attempt") from error

    def matches_native(self, *, turn_id: str, native_id: str, text: str) -> bool:
        return (self.turn_id, self.native_id) == (turn_id, native_id) and self.sent_digest.matches(text)


class BoundUnknownInput(SentInput):
    unresolved = True
    public_status = "unknown"

    def clear_native(self, native_id: str, text: str) -> InputTransition:
        if self.native_id == native_id and self.sent_digest.matches(text):
            return MovedInput(self._transition(ClearedNativeInput))
        return UnchangedInput(self)

    def started(self, *, turn_id: str, native_id: str, text: str) -> InputTransition:
        if self.matches_native(turn_id=turn_id, native_id=native_id, text=text):
            return MovedInput(self._transition(StartedInput))
        return UnchangedInput(self)


class ClearedUnsentInput(ClearedInputState, StoredInput):
    """Explicit withdrawal before any native binding; never a retry grant."""


class ClearedNativeInput(ClearedInputState, SentInput):
    """The native queue confirmed removal; original wire binding stays intact."""


class StartedInput(SentInput):
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

    def delivered_in(self, lease, root_id, snapshot) -> InputDelivery:
        if self.matches_lease(lease, snapshot):
            return self.origin.delivery_reference(root_id)
        return UndeliveredInput()

    def started_for_native(
        self,
        lease: TurnLeaseFence,
        native_id: str,
        sent_text: str,
        *,
        snapshot: RegistrySnapshot,
    ) -> bool:
        """Whether this original row's recorded lease/input evidence is this native start.

        Stored rows record owner name and input generation, not thread birth or process. The
        publication source independently validates the original complete lease
        and native ancestry; this acknowledgement never invents their missing proof.
        """
        return self.matches_lease(lease, snapshot) and self.matches_native(
            turn_id=lease.turn_id, native_id=native_id, text=sent_text)

class NotSentInput(RecoverableUnboundInput):
    unresolved = True
    public_status = "not_sent"
    cancellation_feedback = (
        "Not sent — cancellation completed before native delivery."
    )


class MissingInput(InputAttempt):
    public_status = "missing"
