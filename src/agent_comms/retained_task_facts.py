"""Exact, read-only projections of original task authorities at a source cut.

These values have no update or persistence lifecycle. CompactionSource owns the
captured read; the wire, registry and input document remain the only authorities.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

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
class RetainedTaskFacts:
    """One frozen source projection; no inferred prose facts or replay authority."""

    facts: tuple[ExactTaskFact, ...]

    def current_decisions(self, owner: Thread, registry: RegistrySnapshot) -> tuple[Message, ...]:
        """Resolve explicit original-reference lineage, never equal text or time.

        These local maps live only for this read and have no update lifecycle.
        All original records remain present in the immutable source evidence.
        """
        originals: dict[MessageReference, MessageReference] = {}
        effective: dict[MessageReference, Message] = {}
        for fact in self.facts:
            for message in fact.choices():
                decision = message.require_decision()
                root = decision.change.root_reference(message.reference, originals)
                originals[message.reference] = root
                effective[root] = message
        return tuple(message for message in effective.values()
                     if message.require_decision().applies(owner, registry))

    def for_owner(self, owner: Thread, registry: RegistrySnapshot) -> RetainedTaskFacts:
        """Classify the same original facts at the existing frozen source cut."""
        current = frozenset(message.reference for message in self.current_decisions(owner, registry))
        return RetainedTaskFacts(tuple(fact.for_decisions(current) for fact in self.facts))

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
