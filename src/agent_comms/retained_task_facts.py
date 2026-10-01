"""Exact, read-only projections of original task authorities at a source cut.

These values have no update or persistence lifecycle. CompactionSource owns the
captured read; the wire, registry and input document remain the only authorities.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import FieldCodec
from .goals import Goal
from .input_attempt import StoredInput
from .messages import Message
from .thread_identity import ThreadRole


class ExactTaskFact(DeclaredFamily, affix="TaskFact"):
    """Membership belongs to each determining source, not the packing policy."""


@dataclass(frozen=True)
class UserSourceTaskFact(ExactTaskFact):
    source: Message

    def __post_init__(self) -> None:
        if self.source.sender_role is not ThreadRole.USER:
            raise RelationViolationError("A peer message is not an original user source")


@dataclass(frozen=True)
class DecisionTaskFact(ExactTaskFact):
    source: Message

    def __post_init__(self) -> None:
        self.source.require_decision()


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
