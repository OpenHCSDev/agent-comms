"""Typed durable input evidence and notices; never replay authority."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from typing import ClassVar, Literal

from .errors import RelationViolationError
from .input_attempt import (
    GoalInputDecision,
    InputAttempt,
    MissingInput,
    ReservedInput,
    SentInput,
    StoredInput,
)
from .locked_store import LockedStore
from .messages import Message
from .threads import Thread


class FutureInputQueue(ABC):
    """Process-local queue authority; never reconstructed from UNKNOWN rows."""

    @abstractmethod
    def future_inputs(
        self, owner: Thread, pending_input_key: str | None
    ) -> dict[str, InputAttempt]: ...


@dataclass(frozen=True, slots=True)
class InputDocument:
    rows: dict[str, StoredInput] = field(default_factory=dict, metadata={"wire_required": True})
    version: Literal[1] = field(default=1, metadata={"wire_required": True})

    def __post_init__(self) -> None:
        if any(not row.exists or key != row.key for key, row in self.rows.items()):
            raise ValueError("Input document key differs from its attempt")

    def lookup(self, key: str | None) -> InputAttempt:
        return self.rows.get(key, MissingInput())

    def compaction_rows(
        self, owner: Thread, pending_input_key: str | None, queue: FutureInputQueue | None = None
    ) -> dict[str, InputAttempt]:
        """Caller holds wire; native CAS additionally retains document lock."""
        assert owner.active_turn is not None
        pending = self.lookup(pending_input_key)
        if pending_input_key is not None and not pending.pending_for(owner):
            raise RelationViolationError("Original owner input already attempted")
        future = queue.future_inputs(owner, pending_input_key) if queue is not None else {}
        if any(self.lookup(key) != receipt for key, receipt in future.items()):
            raise RelationViolationError("Queued owner input changed after acceptance")
        relevant = {}
        for key, row in self.rows.items():
            if not row.matches_owner(owner.incarnation):
                continue
            if key != pending_input_key and key in future and row.pending_for(owner):
                continue
            if row.unsettled_for(owner, pending_input_key):
                raise RelationViolationError("Unsettled owner input; compaction not dispatched")
            relevant[key] = row
        return relevant

    def source_texts(self, keys: tuple[str, ...]) -> tuple[str, ...] | None:
        if any(key not in self.rows for key in keys):
            return None
        return tuple(self.rows[key].source_text for key in keys)

    def all_started(self, keys) -> bool:
        return all(self.lookup(key).has_started for key in keys)

    def unknown(self, owners: frozenset[str]) -> tuple[InputAttempt, ...]:
        return tuple(
            sorted(
                (row for row in self.rows.values() if row.owner in owners and row.unresolved),
                key=lambda row: row.order,
            )
        )

    def bound_bus_inputs(self) -> tuple[SentInput, ...]:
        return tuple(
            bound for row in self.rows.values() if (bound := row.bound_bus_input()) is not None
        )

    def delivery_overview(
        self,
        owners: frozenset[str],
        *,
        include_history: bool = False,
        awaiting_keys: frozenset[str] | None = None,
    ) -> dict:
        current, historical = [], []
        dismissed = historical_count = 0
        for row in self.unknown(owners):
            if row.historical_notice(awaiting_keys):
                dismissed += int(row.notice_dismissed)
                historical_count += int(not row.notice_dismissed)
                if include_history:
                    historical.append({**row.public(), "noticeDismissed": row.notice_dismissed})
            else:
                current.append(row.public())
        return {
            "inputs": current,
            "historicalCount": historical_count,
            "dismissedHistoricalCount": dismissed,
            "historicalInputs": historical,
            "currentScope": "owner_queue" if awaiting_keys is not None else "unobserved",
        }


@dataclass(frozen=True, slots=True)
class InputDispositions(LockedStore[InputDocument]):
    filename: ClassVar[str] = "input_dispositions.json"
    json_sort_keys = True

    @property
    def record_type(self) -> type[InputDocument]:
        return InputDocument

    def empty(self) -> InputDocument:
        return InputDocument()

    @staticmethod
    def bus_key(message: Message, owner: Thread) -> str:
        return message.response_policy.disposition_key(message, owner)

    def record(
        self, key: str, *, seq: int | None, owner: str, admission: int, target: str, text: str
    ) -> bool:
        """Return acceptance only after the reservation and directory are fsynced."""
        row = ReservedInput(key, seq, owner, admission, target, text)
        recorded = False

        def change(document: InputDocument) -> InputDocument:
            nonlocal recorded
            if key in document.rows:
                return document
            recorded = True
            return replace(document, rows={**document.rows, key: row})

        self.update(change)
        return recorded

    def _transition(self, key: str, change) -> bool:
        changed = False

        def update(document: InputDocument) -> InputDocument:
            nonlocal changed
            next_row = change(document.lookup(key))
            if next_row is None:
                return document
            changed = True
            return replace(document, rows={**document.rows, key: next_row})

        self.update(update)
        return changed

    def bind(self, key: str, *, admission: int, turn_id: str, native_id: str, text: str) -> bool:
        return self._transition(
            key,
            lambda row: row.bind(
                admission=admission, turn_id=turn_id, native_id=native_id, text=text
            ),
        )

    def started(self, key: str, *, turn_id: str, native_id: str, text: str) -> bool:
        return self._transition(
            key, lambda row: row.started(turn_id=turn_id, native_id=native_id, text=text)
        )

    def finish_unbound(self, key: str) -> bool:
        """Caller ended the owning turn under its wire fence; never permits replay."""
        return self._transition(key, lambda row: row.finish_unbound())

    def review_for_goal(
        self,
        keys: tuple[str, ...],
        *,
        owners: frozenset[str],
        goal_id: str,
        goal_revision: int,
        turn_id: str,
        observed: tuple[InputAttempt, ...] = (),
    ) -> None:
        decision = GoalInputDecision(goal_revision, turn_id)

        def review(document: InputDocument) -> InputDocument:
            rows = dict(document.rows)
            for row in observed:
                if row.key not in keys or (row.key in rows and rows[row.key] != row):
                    raise ValueError("Reviewed inputs changed; inspect them again.")
                rows.setdefault(row.key, row)
            if any(
                key not in rows or rows[key].owner not in owners or not rows[key].unresolved
                for key in keys
            ):
                raise ValueError("Reviewed inputs changed; inspect them again.")
            if not keys:
                return document
            return replace(
                document,
                rows={**rows, **{key: rows[key].review(goal_id, decision) for key in keys}},
            )

        self.update(review)

    def dismiss_historical(
        self,
        owners: frozenset[str],
        *,
        awaiting_keys: frozenset[str] | None = None,
    ) -> dict:
        def dismiss(document: InputDocument) -> InputDocument:
            changed = {
                row.key: replace(row, notice_dismissed=True)
                for row in document.unknown(owners)
                if row.historical_notice(awaiting_keys) and not row.notice_dismissed
            }
            return replace(document, rows={**document.rows, **changed}) if changed else document

        return self.update(dismiss).delivery_overview(owners, awaiting_keys=awaiting_keys)
