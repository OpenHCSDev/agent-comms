"""Durable input observations, never an execution or replay capability."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field, fields, replace
from typing import TYPE_CHECKING, ClassVar
from uuid import UUID, uuid4

from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import FieldCodec, TextRepresentation, projected
from .native_input_record import NativeInputIdText
from .thread_identity import GenerationCounter, ThreadIncarnation
from .input_origin import InputOrigin, InputProvenance, UnattributedInputOrigin
from .threads import Thread

if TYPE_CHECKING:
    from .registry_document import RegistrySnapshot
    from .text_digest import TextDigest
    from .thread_identity import TurnId
    from .turn_lease import TurnLeaseFence


class ACPInputIdText(TextRepresentation):
    """One original submission ID, independent of its later native input ID."""

    @classmethod
    def new(cls) -> str:
        return uuid4().hex

    @classmethod
    def encode(cls, value):
        return cls.decode(value)

    @classmethod
    def from_text(cls, value: str) -> str:
        if UUID(hex=value).hex != value:
            raise ValueError("ACP submission requires a canonical UUID hex identity")
        return value

@dataclass(frozen=True, slots=True)
class GoalInputDecision:
    goal_revision: int
    turn_id: str


@dataclass(frozen=True)
class InputAttempt(DeclaredFamily, affix="Input"):
    exists: ClassVar[bool] = False
    accepts_reservation: ClassVar[bool] = False
    has_started: ClassVar[bool] = False
    has_native_binding: ClassVar[bool] = False
    unresolved: ClassVar[bool] = False
    cancellation_feedback: ClassVar[str] = (
        "Delivery unconfirmed — input not retried. Inspect delivery before deciding whether to send a new message."
    )

    @property
    @abstractmethod
    def public_status(self) -> str: ...

    def matches_owner(self, source_owner: ThreadIncarnation) -> bool:
        return False

    def matches_admission(self, admission: int) -> bool:
        return False

    def queued_for(self, owner: ThreadIncarnation, admission: int, text: str) -> bool:
        return False

    def require_started(self, admission: int) -> StartedInput:
        raise RelationViolationError("Input start lacks its original native disposition")

    def proves_started(
        self,
        *,
        owner: ThreadIncarnation,
        admission: int,
        turn: TurnId,
        sent_digest: TextDigest,
        original_digest: TextDigest,
    ) -> bool:
        return False

    def bind(
        self, *, admission: int, turn_id: str, native_id: str, text: str
    ) -> InputAttempt | None:
        return None

    def started(self, *, turn_id: str, native_id: str, text: str) -> InputAttempt | None:
        return None

    def finish_unbound(self) -> InputAttempt | None:
        return None

    def matches_native(self, *, turn_id: str, native_id: str, text: str) -> bool:
        return False

    def started_for_native(
        self,
        lease: TurnLeaseFence,
        native_id: str,
        sent_text: str,
        *,
        snapshot: RegistrySnapshot,
    ) -> StartedInput | None:
        return None

    def pending_for(self, owner: Thread) -> bool:
        return False

    def bound_bus_input(self) -> SentInput | None:
        return None


@dataclass(frozen=True)
class StoredInput(InputAttempt):
    """Recorded owner name/admission provenance, with no invented incarnation."""

    exists = True

    def context_provenance(self) -> InputProvenance:
        return InputProvenance(self.key, self.origin)
    key: str = field(metadata={"public_exclude": True})
    sequence: int | None
    owner: str = field(metadata={"public_exclude": True})
    admission: int = field(metadata={"public_exclude": True})
    target: str
    source_text: str = field(metadata={"public_name": "text"})
    origin: InputOrigin = field(default_factory=UnattributedInputOrigin, kw_only=True,
                               metadata={"public_exclude": True, "wire_omit_default": True})
    notice_dismissed: bool = field(
        default=False, metadata={"public_exclude": True, "wire_omit_default": True}, kw_only=True
    )
    goal_reviews: dict[str, GoalInputDecision] = field(
        default_factory=dict,
        metadata={"public_exclude": True, "wire_omit_default": True},
        kw_only=True,
    )

    def __post_init__(self) -> None:
        try:
            for value in (self.key, self.owner, self.target, self.source_text):
                if not FieldCodec.decode(str, value):
                    raise ValueError("Input identity text cannot be empty")
            GenerationCounter.require_positive(self.admission)
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
        from .text_digest import TextDigest

        return TextDigest.of(self.source_text)

    def matches_owner(self, source_owner: ThreadIncarnation) -> bool:
        # The selected source must separately match the live full incarnation.
        # Historical rows never recorded creation time and cannot attest it.
        return self.owner == source_owner.name

    def matches_admission(self, admission: int) -> bool:
        return self.admission == admission

    def unsettled_for(self, owner: Thread, pending_key: str | None) -> bool:
        assert owner.active_turn is not None
        admission = owner.active_turn.admission_generation
        return admission is None or (
            self.admission == admission and self.unresolved and self.key != pending_key
        )

    def _transition(self, target: type[StoredInput], **changes) -> StoredInput:
        values = {item.name: getattr(self, item.name) for item in fields(self)}
        return target(**values, **changes)

    @property
    def order(self) -> tuple[bool, int]:
        return self.sequence is None, self.sequence or 0

    def historical_notice(self, awaiting_keys: frozenset[str] | None) -> bool:
        if awaiting_keys is None:
            return self.notice_dismissed
        return self.key not in awaiting_keys

    def reviewed_for_goal(self, goal_id: str) -> bool:
        return goal_id in self.goal_reviews

    def review(self, goal_id: str, decision: GoalInputDecision) -> StoredInput:
        if not self.unresolved:
            raise ValueError("Reviewed inputs changed; inspect them again.")
        return replace(self, goal_reviews={**self.goal_reviews, goal_id: decision})

    @projected(view="public", name="inputId")
    def public_id(self) -> str:
        return self.key.removeprefix("acp:")

    @projected(view="public", name="status")
    def public_state(self) -> str:
        return self.public_status

    def public(self) -> dict:
        return {
            **FieldCodec.project(self, "public"),
            **({"reviewedForGoals": sorted(self.goal_reviews)} if self.goal_reviews else {}),
        }


class ReservedInput(StoredInput):
    unresolved = True
    public_status = "unknown"

    accepts_reservation = True

    def queued_for(self, owner: ThreadIncarnation, admission: int, text: str) -> bool:
        return (
            self.matches_owner(owner)
            and self.matches_admission(admission)
            and self.digest.matches(text)
        )

    def bind(
        self, *, admission: int, turn_id: str, native_id: str, text: str
    ) -> InputAttempt | None:
        if self.admission != admission:
            return None
        return self._transition(
            BoundUnknownInput, turn_id=turn_id, native_id=native_id, sent_text=text
        )

    def pending_for(self, owner: Thread) -> bool:
        assert owner.active_turn is not None
        return self.matches_owner(owner.incarnation) and self.matches_admission(
            owner.active_turn.admission_generation
        )

    def finish_unbound(self) -> NotSentInput:
        return self._transition(NotSentInput)


@dataclass(frozen=True)
class SentInput(StoredInput):
    has_native_binding = True
    turn_id: str = field(metadata={"public_exclude": True, "wire_required": True})
    native_id: str = field(metadata={"public_exclude": True, "wire_required": True})
    sent_text: str = field(metadata={"public_exclude": True, "wire_required": True})

    def __post_init__(self) -> None:
        super().__post_init__()
        try:
            from .thread_identity import TurnId

            TurnId.for_registration(self.turn_id)
            NativeInputIdText.decode(self.native_id)
            if not FieldCodec.decode(str, self.sent_text):
                raise ValueError("Native input text cannot be empty")
        except ValueError as error:
            raise ValueError("Invalid native input attempt") from error

    @property
    def sent_digest(self) -> TextDigest:
        from .text_digest import TextDigest

        return TextDigest.of(self.sent_text)

    def matches_native(self, *, turn_id: str, native_id: str, text: str) -> bool:
        return (self.turn_id, self.native_id, self.sent_text) == (turn_id, native_id, text)

    def bound_bus_input(self) -> SentInput | None:
        return self if self.sequence is not None else None


class BoundUnknownInput(SentInput):
    unresolved = True
    public_status = "unknown"

    def started(self, *, turn_id: str, native_id: str, text: str) -> StartedInput | None:
        return (
            self._transition(StartedInput)
            if self.matches_native(turn_id=turn_id, native_id=native_id, text=text)
            else None
        )


class StartedInput(SentInput):
    has_started = True
    public_status = "started"
    cancellation_feedback = "Native input started; turn cancelled — input not retried."

    def require_started(self, admission: int) -> StartedInput:
        if not self.matches_admission(admission):
            raise RelationViolationError("Input start admission changed")
        return self

    def started_for_native(
        self,
        lease: TurnLeaseFence,
        native_id: str,
        sent_text: str,
        *,
        snapshot: RegistrySnapshot,
    ) -> StartedInput | None:
        """Return this original row's recorded lease/input evidence only.

        Stored rows record name/admission, not thread birth or process. The
        publication source independently validates the original complete lease
        and native ancestry; this receipt never invents their missing proof.
        """
        if not lease.identity.incarnation.matches_recorded_name(self.owner, snapshot):
            return None
        if not self.matches_admission(lease.admission_generation):
            return None
        return (
            self
            if self.matches_native(turn_id=lease.turn_id, native_id=native_id, text=sent_text)
            else None
        )

    def proves_started(
        self,
        *,
        owner: ThreadIncarnation,
        admission: int,
        turn: TurnId,
        sent_digest: TextDigest,
        original_digest: TextDigest,
    ) -> bool:
        return (
            self.matches_owner(owner)
            and self.matches_admission(admission)
            and self.turn_id == turn.value
            and self.sent_digest == sent_digest
            and self.digest == original_digest
        )


class NotSentInput(StoredInput):
    unresolved = True
    public_status = "not_sent"
    cancellation_feedback = (
        "Not sent — cancellation completed before native delivery. Input not retried."
    )

    def unsettled_for(self, owner: Thread, pending_key: str | None) -> bool:
        return False


class MissingInput(InputAttempt):
    public_status = "missing"
