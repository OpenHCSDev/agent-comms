"""Durable input observations, never an execution or replay capability."""

from __future__ import annotations

import re
from abc import abstractmethod
from dataclasses import dataclass, field, replace
from typing import ClassVar

from .declared_family import DeclaredFamily
from .field_codec import FieldCodec, projected
from .threads import Thread

_NATIVE_ID = re.compile(r"[0-9a-f]{32}\Z")


@dataclass(frozen=True, slots=True)
class GoalInputDecision:
    goal_revision: int
    turn_id: str


@dataclass(frozen=True)
class InputAttempt(DeclaredFamily, affix="Input"):
    family_discriminator: ClassVar[str] = "status"
    key: str = field(metadata={"public_exclude": True})
    sequence: int | None
    owner: str = field(metadata={"public_exclude": True})
    admission: int = field(metadata={"public_exclude": True})
    target: str
    source_text: str = field(metadata={"public_name": "text"})
    turn_id: str | None = field(default=None, metadata={"public_exclude": True})
    native_id: str | None = field(default=None, metadata={"public_exclude": True})
    sent_text: str | None = field(default=None, metadata={"public_exclude": True})
    notice_dismissed: bool = field(
        default=False, metadata={"public_exclude": True, "wire_omit_default": True}
    )
    goal_reviews: dict[str, GoalInputDecision] = field(
        default_factory=dict, metadata={"public_exclude": True, "wire_omit_default": True}
    )
    unresolved: ClassVar[bool]

    def __post_init__(self) -> None:
        if (
            not self.key
            or not self.owner
            or not self.target
            or type(self.source_text) is not str
            or not self.source_text
            or self.admission <= 0
        ):
            raise ValueError("Invalid ACP input identity")
        if self.sequence is not None and (
            self.sequence <= 0
            or (
                self.key != f"bus:{self.sequence}"
                and not self.key.startswith(f"bus:{self.sequence}:owner:")
            )
        ):
            raise ValueError("Bus input key and sequence disagree")
        if self.native_id is not None and _NATIVE_ID.fullmatch(self.native_id) is None:
            raise ValueError("Invalid native input attempt")

    @property
    def unattempted(self) -> bool:
        return self.unresolved and self.native_id is None

    def matches_native(self, *, turn_id: str, native_id: str, text: str) -> bool:
        return (self.turn_id, self.native_id, self.sent_text) == (turn_id, native_id, text)

    @abstractmethod
    def bind(
        self, *, admission: int, turn_id: str, native_id: str, text: str
    ) -> InputAttempt | None: ...

    @abstractmethod
    def started(self, *, turn_id: str, native_id: str, text: str) -> InputAttempt | None: ...

    def pending_for(self, owner: Thread) -> bool:
        assert owner.active_turn is not None
        return (
            self.unattempted
            and self.key.startswith("acp:")
            and self.sequence is None
            and self.target == self.owner == owner.name
            and self.admission == owner.active_turn.admission_generation
            and self.turn_id is None
            and self.sent_text is None
        )

    def unsettled_for(self, owner: Thread, pending_key: str | None) -> bool:
        assert owner.active_turn is not None
        admission = owner.active_turn.admission_generation
        return admission is None or (
            self.admission == admission and self.unresolved and self.key != pending_key
        )

    def finish_unbound(self) -> InputAttempt | None:
        """Only an unbound attempt may become a known not-sent notice."""
        return None

    @property
    def order(self) -> tuple[bool, int]:
        return self.sequence is None, self.sequence or 0

    def historical_notice(self, awaiting_keys: frozenset[str] | None) -> bool:
        """Only current owner queue facts can retire an unresolved notice."""
        if awaiting_keys is None:
            return self.notice_dismissed
        return self.key not in awaiting_keys

    def reviewed_for_goal(self, goal_id: str) -> bool:
        return goal_id in self.goal_reviews

    def review(self, goal_id: str, decision: GoalInputDecision) -> InputAttempt:
        if not self.unresolved:
            raise ValueError("Reviewed inputs changed; inspect them again.")
        return replace(self, goal_reviews={**self.goal_reviews, goal_id: decision})

    @projected(view="public", name="inputId")
    def public_id(self) -> str:
        return self.key.removeprefix("acp:")

    @projected(view="public", name="status")
    def public_status(self) -> str:
        return self.declared_name

    def public(self) -> dict:
        return {
            **FieldCodec.project(self, "public"),
            **({"reviewedForGoals": sorted(self.goal_reviews)} if self.goal_reviews else {}),
        }


class UnknownInput(InputAttempt):
    unresolved = True

    def finish_unbound(self) -> InputAttempt | None:
        if self.native_id is not None or self.turn_id is not None or self.sent_text is not None:
            return None
        from dataclasses import fields

        return NotSentInput(**{item.name: getattr(self, item.name) for item in fields(self)})

    def bind(
        self, *, admission: int, turn_id: str, native_id: str, text: str
    ) -> InputAttempt | None:
        if self.admission != admission or self.native_id is not None:
            return None
        if not turn_id or _NATIVE_ID.fullmatch(native_id) is None:
            raise ValueError("Invalid native input attempt")
        return replace(self, turn_id=turn_id, native_id=native_id, sent_text=text)

    def started(self, *, turn_id: str, native_id: str, text: str) -> InputAttempt | None:
        if not self.matches_native(turn_id=turn_id, native_id=native_id, text=text):
            return None
        # Constructor fields derive from the declaration, not a second roster.
        from dataclasses import fields

        return StartedInput(**{item.name: getattr(self, item.name) for item in fields(self)})


class TerminalInput(InputAttempt):
    @property
    @abstractmethod
    def unresolved(self) -> bool: ...

    def bind(self, *, admission: int, turn_id: str, native_id: str, text: str) -> None:
        return None

    def started(self, *, turn_id: str, native_id: str, text: str) -> None:
        return None


class StartedInput(TerminalInput):
    unresolved = False


class NotSentInput(TerminalInput):
    """The turn ended before native binding; retained for explicit user retry."""

    unresolved = True

    @property
    def unattempted(self) -> bool:
        return False

    def unsettled_for(self, owner: Thread, pending_key: str | None) -> bool:
        return False
