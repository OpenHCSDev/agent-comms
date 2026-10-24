"""Typed durable input evidence and notices; never replay authority."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar

from agent_comms import field_codec

from .channel_targets import CommandTarget
from .errors import RelationViolationError
from .field_codec import TurnId, GoalId, RootId
from .input_attempt import (
    UNOBSERVED_DELIVERY_SCOPE,
    GoalInputDecision,
    HistoricalInput,
    InputAttempt,
    InputDeliveryScope,
    MissingInput,
    ReservedInput,
    StartedInput,
    StoredInput,
    UnresolvedInput,
)
from .input_origin import InputOrigin, UnattributedInputOrigin
from .lifecycle_capability import Unresolved
from .locked_store import DocumentChange
from .message_reference import MessageReference
from .messages import Message
from .threads import Thread
from .tool_result import ToolResult
from .reported_turn import ReportedTurnState
from .typed_table import Column, DurableStore, Generated, Index, StoreRows, TypedTable, sql_literal

if TYPE_CHECKING:
    from .bus import BusReader
    from .channel_input_batch import SingleInputBatch
    from .private_registry_guard import RegistryPrivacy
    from .registry_document import RegistrySnapshot
    from .turn_lease import TurnLeaseFence


@dataclass(frozen=True)
class DeliveryOverview(ToolResult):
    """Current unresolved inputs and earlier notices; the ``owner_queue`` scope was checked
    against the owner."""

    inputs: tuple[UnresolvedInput, ...]
    historical_count: Annotated[int, field_codec.WireName("historicalCount")]
    dismissed_historical_count: Annotated[
        int, field_codec.WireName("dismissedHistoricalCount")
    ]
    historical_inputs: Annotated[
        tuple[HistoricalInput, ...], field_codec.WireName("historicalInputs")
    ]
    current_scope: Annotated[
        type[InputDeliveryScope], field_codec.WireName("currentScope")
    ]


@dataclass(frozen=True, slots=True)
class InputDocument:
    """The stored rows one query selected; never the whole store."""

    rows: dict[str, StoredInput] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if any(not row.exists or key != row.key for key, row in self.rows.items()):
            raise ValueError("Input document key differs from its attempt")

    def lookup(self, key: str | None) -> InputAttempt:
        return self.rows.get(key, MissingInput())

    def record(self, *originals: ReservedInput) -> InputDocument:
        """An existing original cannot be replaced by another reservation."""
        additions = {}
        for row in originals:
            if row.key not in self.rows:
                additions.setdefault(row.key, row)
        return replace(self, rows={**self.rows, **additions}) if additions else self

    def originals(self, keys: tuple[str, ...]) -> tuple[StoredInput, ...]:
        """Capture ordered originals from their sole durable declaration owner."""
        if len(set(keys)) != len(keys):
            raise ValueError("Original input membership contains duplicate keys")
        try:
            return tuple(self.rows[key] for key in keys)
        except KeyError as error:
            raise ValueError("Original input acknowledgement is unavailable") from error

    def transition(self, keys: tuple[str, ...], change) -> InputDocument:
        """Each original state decides its successor; unchanged rows stay intact."""
        successors = {
            name: row for key in keys for name, row in change(self.lookup(key)).successors(key).items()
        }
        return replace(self, rows={**self.rows, **successors}) if successors else self

    def settle_unbound(self, keys: tuple[str, ...]) -> InputDocument:
        return self.transition(keys, lambda row: row.finish_unbound())

    def started_for_native(
        self, lease: TurnLeaseFence, native_id: str, sent_text: str,
        *, snapshot: RegistrySnapshot,
    ) -> tuple[StartedInput, ...]:
        """Original rows for one actual native user, including grouped inputs.

        Corrections and followups may own the final assistant's closest user.
        Look up that producer's exact native ID/text rather than choosing the
        initial turn input, a public event or the first matching row.
        """
        return tuple(row for row in self.rows.values()
                     if row.started_for_native(lease, native_id, sent_text, snapshot=snapshot))

    def shared_state(self, keys: tuple[str, ...]) -> type[InputAttempt] | None:
        """Project only a complete, homogeneous durable observation."""
        states = {type(self.lookup(key)) for key in keys}
        if len(states) == 1 and all(self.lookup(key).exists for key in keys):
            return states.pop()
        return None

    def all_started(self, keys) -> bool:
        return all(self.lookup(key).has_started for key in keys)

    def unknown(self, owners: frozenset[str]) -> tuple[StoredInput, ...]:
        return tuple(
            sorted(
                (pending for row in self.rows.values()
                 for pending in row.items_with(Unresolved, row) if pending.owner in owners),
                key=lambda row: row.order,
            )
        )


    def answered_by(
        self, lease: TurnLeaseFence, source: BusReader, snapshot: RegistrySnapshot,
        target: str, *, privacy: RegistryPrivacy,
    ) -> tuple[MessageReference, ...]:
        """The bus messages the lease's turn took in that a message to target answers."""
        delivered = tuple(
            reference
            for row in sorted(self.rows.values(), key=lambda item: item.order)
            for reference in row.delivered_in(lease, privacy.namespace, snapshot).references()
        )
        route = CommandTarget.named(target).canonical_name(snapshot.canonical_name)
        return tuple(
            original.message.reference
            for original in source.messages_for_references(delivered)
            if original.message.exact_reply_target == route
        )

    def unbound_wire_inputs(self, root_id: RootId) -> dict[MessageReference, StoredInput]:
        """Original bus deliveries with positive evidence that Pi never received them."""
        return {
            reference: row
            for row in self.rows.values()
            for reference in row.unbound_delivery(root_id).references()
        }

    def delivery_overview(
        self,
        owners: frozenset[str],
        *,
        include_history: bool = False,
        scope: InputDeliveryScope = UNOBSERVED_DELIVERY_SCOPE,
    ) -> DeliveryOverview:
        current: list[UnresolvedInput] = []
        historical: list[HistoricalInput] = []
        dismissed = historical_count = 0
        for row in self.unknown(owners):
            if scope.historical_notice(row):
                dismissed += int(row.notice_dismissed)
                historical_count += int(not row.notice_dismissed)
                if include_history:
                    historical.append(
                        HistoricalInput.notice(row, row.notice_dismissed))
            else:
                current.append(UnresolvedInput.of(row))
        return DeliveryOverview(
            tuple(current), historical_count, dismissed, tuple(historical), type(scope))


def _unresolved_kinds() -> str:
    """The unresolved members' stored names, derived from their declarations."""
    return "(" + ", ".join(
        sql_literal(member) for member in InputAttempt.members_with(Unresolved)
    ) + ")"


class InputDispositionsRows(StoreRows):
    """The tables of InputDispositions; each row declares itself one."""


@dataclass(frozen=True)
class InputDispositionRow(InputDispositionsRows, TypedTable):
    """One stored input; its lookup columns are generated from the stored row."""

    key: Annotated[str, Column(primary_key=True)]
    attempt: StoredInput
    owner: ClassVar[Generated] = Generated(str, Column(generated="json_extract(attempt,'$.owner')"))
    turn_id: ClassVar[Generated] = Generated(
        str,
        Column(generated="json_extract(attempt,'$.turn_id')", nullable=True),
    )
    sequence: ClassVar[Generated] = Generated(
        int,
        Column(generated="json_extract(attempt,'$.sequence')", nullable=True),
    )
    unresolved: ClassVar[Generated] = Generated(
        bool,
        Column(generated=f"json_extract(attempt,'$.kind') IN {_unresolved_kinds()}"),
    )
    checks = ("key = json_extract(attempt,'$.key')",)
    indexes = (
        Index(("owner", "unresolved")),
        Index(("turn_id",), where="turn_id IS NOT NULL"),
    )


def _members(column: str, values) -> tuple[str, tuple]:
    values = tuple(values)
    return f'"{column}" IN ({", ".join("?" for _ in values)})', values


@dataclass(frozen=True, slots=True)
class InputDispositions(DurableStore):
    """Indexed durable input rows; each operation reads only the rows it names."""

    root: Path
    filename: ClassVar[str] = "input_dispositions.sqlite3"
    tables = InputDispositionsRows

    @staticmethod
    def _document(connection: sqlite3.Connection, where: str, parameters: tuple) -> InputDocument:
        return InputDocument({row.key: row.attempt for row in InputDispositionRow.select(
            connection, where=where, parameters=parameters)})

    def _select(self, where: str, parameters: tuple = ()) -> InputDocument:
        with self.snapshot() as connection:
            return self._document(connection, where, parameters)

    def _update[R](
        self, where: str, parameters: tuple,
        change: Callable[[InputDocument], DocumentChange[InputDocument, R]],
    ) -> R:
        """Change only the selected rows; the callback returns the original to skip writing."""
        with self.writing() as connection:
            original = self._document(connection, where, parameters)
            changed = change(original)
            for key, row in changed.document.rows.items():
                if original.rows.get(key) is not row:
                    InputDispositionRow(key, row).upsert(connection)
            return changed.result

    def read(self, keys: tuple[str, ...]) -> InputDocument:
        return self._select(*_members("key", keys))

    def lookup(self, key: str) -> InputAttempt:
        return self.read((key,)).lookup(key)

    def unresolved(self, owners: frozenset[str]) -> InputDocument:
        where, parameters = _members("owner", sorted(owners))
        return self._select(f"unresolved = 1 AND {where}", parameters)

    def in_turn(self, turn_id: TurnId) -> InputDocument:
        return self._select('"turn_id" = ?', (turn_id,))


    @staticmethod
    def bus_key(message: Message, owner: Thread) -> str:
        return message.role.response_policy(message).disposition_key(message, owner)

    def record(
        self, key: str, *, seq: int | None, owner: str, claim_scope: int, target: str, text: str,
        origin: InputOrigin = UnattributedInputOrigin(), ownership: ExitStack | None = None,
    ) -> bool:
        """Register supplied reservation rollback before publishing acceptance."""
        row = ReservedInput(key, seq, owner, claim_scope, target, text,
                            origin=origin)
        document = self.record_originals(row, ownership=ownership)
        return document.rows[key] is row

    def record_originals(
        self, *originals: ReservedInput, ownership: ExitStack | None = None,
    ) -> InputDocument:
        """Publish one original cohort; return its exact captured document."""
        def reserve(original: InputDocument) -> DocumentChange[InputDocument, InputDocument]:
            changed = original.record(*originals)
            if changed is not original and ownership is not None:
                ownership.callback(
                    self.settle_unbound,
                    tuple(key for key in changed.rows if key not in original.rows),
                )
            return DocumentChange.to(changed)

        return self._update(*_members("key", (row.key for row in originals)), reserve)

    def reserve_turn(
        self, owner: str, turn: TurnId, claim_scope: int, text: str,
        *, ownership: ExitStack,
    ) -> SingleInputBatch:
        """Enlist rollback before publication; caller retains the original wire scope."""
        from .channel_input_batch import SingleInputBatch

        row = ReservedInput(f"turn:{turn}", None, owner, claim_scope, owner, text)

        document = self.reserve_originals(row, ownership=ownership)
        return SingleInputBatch(document.originals((row.key,)))

    def reserve_originals(
        self, *originals: ReservedInput, ownership: ExitStack,
    ) -> InputDocument:
        """Require fresh originals, retaining the exact publication and rollback."""
        document = self.record_originals(*originals, ownership=ownership)
        if any(document.rows[row.key] is not row for row in originals):
            raise RelationViolationError("Input reservation already exists")
        return document

    def _transition(self, key: str, change) -> bool:
        def update(document: InputDocument) -> DocumentChange[InputDocument, bool]:
            successor = document.transition((key,), change)
            return DocumentChange(successor, successor is not document)

        return self._update(*_members("key", (key,)), update)

    def clear_unbound(self, keys: tuple[str, ...]) -> InputDocument:
        return self._update(
            *_members("key", keys),
            lambda document: DocumentChange.to(
                document.transition(keys, lambda row: row.clear_unbound())
            ),
        )

    def clear_native(self, key: str, native_id: str, text: str) -> None:
        """Clear only the matching native input; publish nothing if it differs."""
        refusal = "Native clear differs from its original input"
        self._transition(key, lambda row: row.clear_native(native_id, text).require_moved(refusal))

    def bind(
        self, key: str, *, claim_scope: int, turn_id: TurnId, native_id: str, text: str
    ) -> bool:
        return self.bind_originals(
            (key,), claim_scope=claim_scope, turn_id=turn_id, native_id=native_id, text=text
        )

    def bind_turn_input(
        self, keys: tuple[str, ...], *, claim_scope: int, turn_id: TurnId,
        native_id: str, text: str,
        already_bound: bool,
    ) -> bool:
        """Bind a turn's unresolved inputs to the native input carrying them.

        ``already_bound`` (a send-now re-send) requires the existing binding to match.
        """
        document = self.read(keys)
        for row in document.rows.values():
            row.require_not_cleared()
        if any(not document.lookup(key).unresolved_claim(claim_scope) for key in keys):
            return False
        if already_bound:
            return all(document.lookup(key).matches_native(
                native_id=native_id, turn_id=turn_id, text=text
            ) for key in keys)
        return self.bind_originals(
            keys, claim_scope=claim_scope, turn_id=turn_id, native_id=native_id, text=text
        )

    def bind_originals(
        self, keys: tuple[str, ...], *, claim_scope: int, turn_id: TurnId, native_id: str, text: str
    ) -> bool:
        """Bind the complete captured original atomically; no partial batch grant."""
        def change(document: InputDocument) -> DocumentChange[InputDocument, bool]:
            if not keys or len(set(keys)) != len(keys):
                return DocumentChange(document, False)
            successors = {name: row for key in keys for name, row in document.lookup(key).bind(
                claim_scope=claim_scope, turn_id=turn_id, native_id=native_id, text=text
            ).successors(key).items()}
            if successors.keys() != set(keys):
                return DocumentChange(document, False)
            return DocumentChange(replace(document, rows={**document.rows, **successors}), True)

        return self._update(*_members("key", keys), change)

    def started(self, key: str, *, turn_id: TurnId, native_id: str, text: str) -> bool:
        return self._transition(
            key, lambda row: row.started(turn_id=turn_id, native_id=native_id, text=text)
        )

    def settle_unbound(self, keys: tuple[str, ...]) -> InputDocument:
        """Publish one complete retirement and return that exact document cut."""
        return self._update(
            *_members("key", keys), lambda document: DocumentChange.to(document.settle_unbound(keys))
        )

    def review_for_goal(
        self,
        keys: tuple[str, ...],
        *,
        owners: frozenset[str],
        goal_id: GoalId,
        goal_revision: int,
        turn_id: ReportedTurnState,
        observed: tuple[InputAttempt, ...] = (),
    ) -> None:
        decision = GoalInputDecision(goal_revision, turn_id)

        def review(document: InputDocument) -> DocumentChange[InputDocument, InputDocument]:
            rows = dict(document.rows)
            for row in observed:
                if row.key not in keys or (row.key in rows and rows[row.key] != row):
                    raise ValueError("Reviewed inputs changed; inspect them again.")
                rows.setdefault(row.key, row)
            if any(
                key not in rows or rows[key].owner not in owners
                for key in keys
            ):
                raise ValueError("Reviewed inputs changed; inspect them again.")
            if not keys:
                return DocumentChange.to(document)
            return DocumentChange.to(replace(
                document,
                rows={**rows, **{key: rows[key].review(goal_id, decision) for key in keys}},
            ))

        self._update(*_members("key", keys), review)

    def dismiss_historical(
        self,
        owners: frozenset[str],
        *,
        scope: InputDeliveryScope = UNOBSERVED_DELIVERY_SCOPE,
    ) -> DeliveryOverview:
        def dismiss(document: InputDocument) -> DocumentChange[InputDocument, InputDocument]:
            changed = {
                row.key: replace(row, notice_dismissed=True)
                for row in document.unknown(owners)
                if scope.historical_notice(row) and not row.notice_dismissed
            }
            return DocumentChange.to(
                replace(document, rows={**document.rows, **changed}) if changed else document
            )

        where, parameters = _members("owner", sorted(owners))
        return self._update(f"unresolved = 1 AND {where}", parameters, dismiss).delivery_overview(
            owners, scope=scope)
