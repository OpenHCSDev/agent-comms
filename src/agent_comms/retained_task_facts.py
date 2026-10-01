"""Exact, read-only projections of original task authorities at a source cut.

These values have no update or persistence lifecycle. CompactionSource owns the
captured read; the wire, registry and input document remain the only authorities.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import FieldCodec
from .goals import Goal
from .input_attempt import StoredInput
from .messages import Message
from .message_reference import MessageReference
from .thread_identity import ThreadRole

if TYPE_CHECKING:
    from .registry_document import RegistrySnapshot
    from .threads import Thread


class ExactTaskFact(DeclaredFamily, affix="TaskFact"):
    """Membership belongs to each determining source, not the packing policy."""

    def choices(self) -> tuple[Message, ...]:
        return ()

    def for_decisions(self, current: frozenset[MessageReference]) -> ExactTaskFact:
        return self

    def for_owner(self, owner: Thread, registry: RegistrySnapshot) -> ExactTaskFact:
        return self


@dataclass(frozen=True)
class UserSourceTaskFact(ExactTaskFact):
    source: Message

    def __post_init__(self) -> None:
        if self.source.sender_role is not ThreadRole.USER:
            raise RelationViolationError("A peer message is not an original user source")


@dataclass(frozen=True)
class DecisionTaskFact(ExactTaskFact, declared_name="historical_decision"):
    source: Message

    def __post_init__(self) -> None:
        self.source.require_decision()

    def choices(self) -> tuple[Message, ...]:
        return (self.source,)

    def for_decisions(self, current: frozenset[MessageReference]) -> ExactTaskFact:
        if self.source.reference in current:
            return CurrentDecisionTaskFact(self.source)
        return DecisionTaskFact(self.source)


@dataclass(frozen=True)
class CurrentDecisionTaskFact(DecisionTaskFact, declared_name="current_decision"):
    """An original choice selected by the captured registry and wire lineage."""


@dataclass(frozen=True)
class UserDecisionCorrectionTaskFact(UserSourceTaskFact, declared_name="historical_user_correction"):
    def __post_init__(self) -> None:
        super().__post_init__()
        self.source.decision.require_user_supersession()

    def choices(self) -> tuple[Message, ...]:
        return (self.source,)

    def for_decisions(self, current: frozenset[MessageReference]) -> ExactTaskFact:
        if self.source.reference in current:
            return CurrentUserDecisionCorrectionTaskFact(self.source)
        return UserDecisionCorrectionTaskFact(self.source)


@dataclass(frozen=True)
class CurrentUserDecisionCorrectionTaskFact(UserDecisionCorrectionTaskFact,
                                            declared_name="current_user_correction"):
    """The exact human correction selected through its original decision scope."""


@dataclass(frozen=True)
class ClaimTaskFact(ExactTaskFact):
    source: Message

    def __post_init__(self) -> None:
        self.source.require_claim_transition()


@dataclass(frozen=True)
class GoalTaskFact(ExactTaskFact):
    source: Goal


@dataclass(frozen=True)
class InputTaskFact(ExactTaskFact):
    source: StoredInput


@dataclass(frozen=True)
class HumanInputTaskFact(InputTaskFact, declared_name="historical_human_input"):
    """Exact human input with recorded scope; no inferred prose constraint kind."""

    def __post_init__(self):
        self.source.origin.require_human()

    def for_owner(self, owner, registry):
        if self.source.origin.require_human().applies(owner, registry):
            return CurrentHumanInputTaskFact(self.source)
        return HumanInputTaskFact(self.source)


@dataclass(frozen=True)
class CurrentHumanInputTaskFact(HumanInputTaskFact, declared_name="current_human_input"):
    pass


@dataclass(frozen=True)
class RetainedTaskFacts:
    """One frozen source projection; no inferred prose facts or replay authority."""

    facts: tuple[ExactTaskFact, ...]

    journal_control_bytes: ClassVar[int] = 65536

    @classmethod
    def frame_journal(
        cls, record: dict[str, Any], *, payload_path: tuple[str, ...] = ()
    ) -> str:
        """Bound journal controls independently of the exact retained payload.

        The containing record declares its retained-content coordinate. Native
        CompactionPolicy admits that content against the actual selected model;
        a journal control limit is not another model/context budget. The frozen
        payload remains in the same record, byte-for-byte under the existing
        canonical serialization. No content is shortened or stored elsewhere.

        An outcome has no retained payload coordinate: its entire observation,
        including an UNKNOWN reason, is control metadata. Read-only source
        projections are measured here without promoting them to fact authority.
        """
        control = record
        for key in payload_path:
            control = control[key]
            if not isinstance(control, dict):
                raise ValueError("Declared retained journal payload must be an object")
        payload = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)
        content_bytes = (
            len(json.dumps(control, sort_keys=True, separators=(",", ":"),
                           allow_nan=False).encode()) - len("null")
            if payload_path else 0
        )
        if len(payload.encode()) - content_bytes > cls.journal_control_bytes:
            raise ValueError("Compaction journal control metadata exceeds bound")
        return payload

    def current_decisions(self, owner: Thread, registry: RegistrySnapshot) -> tuple[Message, ...]:
        """Resolve explicit original-reference lineage, never equal text or time.

        These local maps live only for this read and have no update lifecycle.
        All original records remain present in the immutable source evidence.
        """
        originals: dict[MessageReference, Message] = {}
        effective: dict[MessageReference, tuple[Message, Message]] = {}
        for fact in self.facts:
            for message in fact.choices():
                for root in message.decision.current_roots(message, originals, owner, registry):
                    originals[message.reference] = root
                    _, previous = effective.get(root.reference, (root, root))
                    if message.decision.revises_after(previous):
                        effective[root.reference] = (root, message)
        return tuple(message for root, message in effective.values()
                     if root.require_decision().applies(owner, registry))

    def for_owner(self, owner: Thread, registry: RegistrySnapshot) -> RetainedTaskFacts:
        """Classify the same original facts at the existing frozen source cut."""
        current = frozenset(message.reference for message in self.current_decisions(owner, registry))
        return RetainedTaskFacts(tuple(fact.for_decisions(current).for_owner(owner, registry)
                                       for fact in self.facts))

    @property
    def text(self) -> str:
        return (
            "<exact-task-source>\n"
            + json.dumps(FieldCodec.encode(self), ensure_ascii=False, sort_keys=True)
            + "\n</exact-task-source>"
        )

    def require_summary(self, summary: str) -> None:
        if not summary.startswith(self.text + "\n\n"):
            raise RelationViolationError("Native summary omitted its exact task source")
