"""Typed durable input evidence and notices; never replay authority."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, ClassVar, Literal

from .errors import RelationViolationError
from .input_origin import InputOrigin, UnattributedInputOrigin
from .input_attempt import (
    GoalInputDecision,
    InputAttempt,
    MissingInput,
    ReservedInput,
    SentInput,
    StartedInput,
    StoredInput,
)
from .locked_store import LockedStore
from .messages import Message
from .threads import Thread

if TYPE_CHECKING:
    from .registry_document import RegistrySnapshot
    from .input_origin import InputProvenance
    from .selected_source import SelectedSource
    from .thread_identity import TurnId
    from .turn_lease import TurnLeaseFence


class FutureInputQueue(ABC):
    """Process-local queue authority; never reconstructed from UNKNOWN rows."""

    def compaction_inputs(
        self, source: SelectedSource, owner: Thread, inputs: InputDocument
    ) -> InputDocument:
        """Current live queue evidence, checked under the wire/document boundary."""
        from .reservation_rules import CommitReservationCheck
        from .thread_identity import TurnId

        if owner.active_turn is None:
            raise RelationViolationError("Selected owner has no active turn")
        CommitReservationCheck(
            source=source,
            revision=source.reserved_revision,
            incarnation=owner.incarnation,
            owner=owner.process_identity,
            turn=TurnId(owner.active_turn.id),
            pending_input_keys=source.pending_input_keys,
        ).require_valid()
        return replace(inputs, rows=inputs.compaction_rows(owner, source.pending_input_keys, self))

    @abstractmethod
    def future_inputs(
        self, owner: Thread, pending_input_keys: tuple[str, ...]
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

    def originals(self, keys: tuple[str, ...]) -> tuple[StoredInput, ...]:
        """Capture ordered originals from their sole durable declaration owner."""
        if len(set(keys)) != len(keys):
            raise ValueError("Original input membership contains duplicate keys")
        try:
            return tuple(self.rows[key] for key in keys)
        except KeyError as error:
            raise ValueError("Original input receipt is unavailable") from error

    def original_provenances(self, keys: tuple[str, ...]) -> tuple[InputProvenance, ...]:
        """Source proofs reference originals without copying mutable disposition data."""
        return tuple(row.context_provenance() for row in self.originals(keys))

    def compaction_rows(
        self, owner: Thread, pending_input_keys: tuple[str, ...], queue: FutureInputQueue | None = None
    ) -> dict[str, StoredInput]:
        """Caller holds wire; native CAS additionally retains document lock."""
        assert owner.active_turn is not None
        if len(set(pending_input_keys)) != len(pending_input_keys) or any(
            not self.lookup(key).pending_for(owner) for key in pending_input_keys
        ):
            raise RelationViolationError("Original owner input already attempted")
        future = queue.future_inputs(owner, pending_input_keys) if queue is not None else {}
        if any(self.lookup(key) != receipt for key, receipt in future.items()):
            raise RelationViolationError("Queued owner input changed after acceptance")
        relevant = {}
        for key, row in self.rows.items():
            if not row.matches_owner(owner.incarnation):
                continue
            if key not in pending_input_keys and key in future and row.pending_for(owner):
                continue
            if row.unsettled_for(owner, key if key in pending_input_keys else None):
                raise RelationViolationError("Unsettled owner input; compaction not dispatched")
            relevant[key] = row
        return relevant

    def compaction_material(self, owner: Thread, pending_input_keys: tuple[str, ...],
                            queue: FutureInputQueue | None):
        """Project exactly the input source selected by existing queue custody.

        Unadmitted future inputs remain in their original durable queue; they
        cannot become the source of an earlier native checkpoint.
        """
        rows = self.compaction_rows(owner, pending_input_keys, queue)
        return rows, self.retained_task_facts(tuple(rows))

    def retained_task_facts(self, keys: tuple[str, ...]):
        """The original input origin owns fact membership and provenance."""
        return tuple(row.origin.retained_fact(row) for row in self.originals(keys))

    def owner_originals(self, owner: Thread) -> tuple[str, ...]:
        """Observe durable membership without selecting a compaction queue.

        Reserved and UNKNOWN rows keep their exact recorded disposition. Only
        compaction_rows, with the live queue's custody, can admit a source cut.
        """
        return tuple(key for key, row in self.rows.items()
                     if row.matches_owner(owner.incarnation))

    def started_for_native(
        self, lease: TurnLeaseFence, native_id: str, sent_text: str,
        *, snapshot: RegistrySnapshot,
    ) -> tuple[StartedInput, ...]:
        """Original rows for one actual native user, including grouped inputs.

        Corrections and followups may own the final assistant's closest user.
        Look up that producer's exact native ID/text rather than choosing the
        initial turn input, a public event or the first matching row.
        """
        return tuple(receipt for row in self.rows.values()
                     if (receipt := row.started_for_native(
                         lease, native_id, sent_text, snapshot=snapshot
                     )) is not None)

    def shared_state(self, keys: tuple[str, ...]) -> type[InputAttempt] | None:
        """Project only a complete, homogeneous durable observation."""
        states = {type(self.lookup(key)) for key in keys}
        if len(states) == 1 and all(self.lookup(key).exists for key in keys):
            return states.pop()
        return None

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
        self, key: str, *, seq: int | None, owner: str, admission: int, target: str, text: str,
        origin: InputOrigin = UnattributedInputOrigin(),
    ) -> bool:
        """Return acceptance only after the reservation and directory are fsynced."""
        row = ReservedInput(key, seq, owner, admission, target, text,
                            origin=origin)
        recorded = False

        def change(document: InputDocument) -> InputDocument:
            nonlocal recorded
            if key in document.rows:
                return document
            recorded = True
            return replace(document, rows={**document.rows, key: row})

        self.update(change)
        return recorded

    def reserve_turn(self, owner: str, turn: TurnId, admission: int, text: str) -> str:
        """Reserve one original with no external ingress in the existing input store."""
        key = f"turn:{turn.value}"
        if not self.record(
            key, seq=None, owner=owner, admission=admission, target=owner, text=text
        ):
            raise RelationViolationError("Original turn input was already reserved")
        return key

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
        return self.bind_originals(
            (key,), admission=admission, turn_id=turn_id, native_id=native_id, text=text
        )

    def bind_originals(
        self, keys: tuple[str, ...], *, admission: int, turn_id: str, native_id: str, text: str
    ) -> bool:
        """Bind the complete captured original atomically; no partial batch grant."""
        bound = False

        def change(document: InputDocument) -> InputDocument:
            nonlocal bound
            if not keys or len(set(keys)) != len(keys):
                return document
            successors = tuple(document.lookup(key).bind(
                admission=admission, turn_id=turn_id, native_id=native_id, text=text
            ) for key in keys)
            if any(row is None for row in successors):
                return document
            bound = True
            return replace(document, rows={**document.rows, **dict(zip(keys, successors, strict=True))})

        self.update(change)
        return bound

    def started(self, key: str, *, turn_id: str, native_id: str, text: str) -> bool:
        return self._transition(
            key, lambda row: row.started(turn_id=turn_id, native_id=native_id, text=text)
        )

    def settle_unbound(self, keys: tuple[str, ...]) -> bool:
        """Terminal caller holds wire; settle one complete batch atomically."""
        changed = False

        def settle(document: InputDocument) -> InputDocument:
            nonlocal changed
            rows = dict(document.rows)
            for key in keys:
                next_row = document.lookup(key).finish_unbound()
                if next_row is not None:
                    changed = True
                    rows[key] = next_row
            return replace(document, rows=rows) if changed else document

        self.update(settle)
        return changed

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
