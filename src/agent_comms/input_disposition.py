"""Typed durable input evidence and notices; never replay authority."""

from __future__ import annotations

import json
import sqlite3
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal

from .errors import RelationViolationError
from .field_codec import FieldCodec
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
from .message_reference import MessageReference
from .messages import Message
from .store_files import _store_lock
from .threads import Thread

if TYPE_CHECKING:
    from .channel_input_batch import SingleInputBatch
    from .registry_document import RegistrySnapshot
    from .thread_identity import TurnId
    from .turn_lease import TurnLeaseFence


@dataclass(frozen=True, slots=True)
class InputDocument:
    rows: dict[str, StoredInput] = field(default_factory=dict, metadata={"wire_required": True})
    version: Literal[1] = field(default=1, metadata={"wire_required": True})

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
            raise ValueError("Original input receipt is unavailable") from error

    def settle_unbound(self, keys: tuple[str, ...]) -> InputDocument:
        """Original members decide retirement; bound/started evidence stays intact."""
        successors = {
            key: successor
            for key in keys
            if (successor := self.lookup(key).finish_unbound()) is not None
        }
        return replace(self, rows={**self.rows, **successors}) if successors else self

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

    def unbound_wire_inputs(self, root_id: str) -> dict[MessageReference, StoredInput]:
        """Original bus deliveries with positive evidence that Pi never received them."""
        return {
            reference: row
            for row in self.rows.values()
            if row.proves_no_native_send
            and (reference := row.origin.delivery_reference(root_id)) is not None
        }

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
class InputDispositions:
    """Key-addressed durable input rows; decoding never holds the SQLite lock."""

    path: Path
    filename: ClassVar[str] = "input_dispositions.sqlite3"

    def __post_init__(self) -> None:
        if self.path.exists():
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with _store_lock(self.path):
            if self.path.exists():
                return
            with self._connect() as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute(
                    """CREATE TABLE input_dispositions (
                        key TEXT PRIMARY KEY,
                        owner TEXT NOT NULL,
                        admission INTEGER NOT NULL,
                        turn_id TEXT,
                        unresolved INTEGER NOT NULL,
                        sequence INTEGER,
                        payload TEXT NOT NULL
                    )"""
                )
                db.execute(
                    "CREATE INDEX input_dispositions_lease "
                    "ON input_dispositions(owner, admission, turn_id)"
                )
                db.execute(
                    "CREATE INDEX input_dispositions_pending "
                    "ON input_dispositions(owner, unresolved, sequence)"
                )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def _read_connection(self) -> sqlite3.Connection:
        return sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=0)

    @staticmethod
    def _payload(row: StoredInput) -> str:
        return json.dumps(FieldCodec.encode(row), sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _decode(payload: str) -> StoredInput:
        return FieldCodec.decode(StoredInput, json.loads(payload))

    @staticmethod
    def _columns(row: StoredInput) -> tuple[object, ...]:
        return (
            row.key,
            row.owner,
            row.admission,
            row.bound_turn_id,
            int(row.unresolved),
            row.sequence,
            InputDispositions._payload(row),
        )

    def _select(self, where: str = "", parameters: tuple[object, ...] = ()) -> InputDocument:
        sql = "SELECT payload FROM input_dispositions"
        if where:
            sql += f" WHERE {where}"
        sql += " ORDER BY sequence IS NULL, sequence, rowid"
        with self._read_connection() as db:
            payloads = tuple(db.execute(sql, parameters))
        rows = tuple(self._decode(payload) for payload, in payloads)
        return InputDocument({row.key: row for row in rows})

    def read(self) -> InputDocument:
        return self._select()

    def rows(self, keys: tuple[str, ...]) -> InputDocument:
        if not keys:
            return InputDocument()
        placeholders = ",".join("?" for _ in keys)
        return self._select(f"key IN ({placeholders})", keys)

    @contextmanager
    def reading(self):
        yield self.read()

    def _publish(self, original: InputDocument, changed: InputDocument) -> InputDocument:
        if changed is original or changed == original:
            return original
        before, after = original.rows, changed.rows
        removed = before.keys() - after.keys()
        replaced = {key for key in after if key not in before or after[key] != before[key]}
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for key in removed:
                cursor = db.execute(
                    "DELETE FROM input_dispositions WHERE key = ? AND payload = ?",
                    (key, self._payload(before[key])),
                )
                if cursor.rowcount != 1:
                    raise RelationViolationError("Input disposition changed during update")
            for key in replaced:
                row = after[key]
                columns = self._columns(row)
                if key in before:
                    cursor = db.execute(
                        """UPDATE input_dispositions
                           SET owner=?, admission=?, turn_id=?, unresolved=?, sequence=?, payload=?
                           WHERE key=? AND payload=?""",
                        (*columns[1:], key, self._payload(before[key])),
                    )
                    if cursor.rowcount != 1:
                        raise RelationViolationError("Input disposition changed during update")
                else:
                    db.execute(
                        "INSERT INTO input_dispositions "
                        "(key, owner, admission, turn_id, unresolved, sequence, payload) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        columns,
                    )
        return changed

    def update(self, change) -> InputDocument:
        original = self.read()
        return self._publish(original, change(original))

    def update_rows(self, keys: tuple[str, ...], change) -> InputDocument:
        original = self.rows(keys)
        return self._publish(original, change(original))

    @staticmethod
    def bus_key(message: Message, owner: Thread) -> str:
        return message.response_policy.disposition_key(message, owner)

    def record(
        self, key: str, *, seq: int | None, owner: str, admission: int, target: str, text: str,
        origin: InputOrigin = UnattributedInputOrigin(), custody: ExitStack | None = None,
    ) -> bool:
        """Enlist supplied reservation custody before publishing acceptance."""
        row = ReservedInput(key, seq, owner, admission, target, text,
                            origin=origin)
        document = self.record_originals(row, custody=custody)
        return document.rows[key] is row

    def record_originals(
        self, *originals: ReservedInput, custody: ExitStack | None = None,
    ) -> InputDocument:
        """Publish one original cohort; return its exact captured document."""
        def reserve(original: InputDocument) -> InputDocument:
            changed = original.record(*originals)
            if changed is not original and custody is not None:
                custody.callback(
                    self.settle_unbound,
                    tuple(key for key in changed.rows if key not in original.rows),
                )
            return changed

        return self.update_rows(tuple(row.key for row in originals), reserve)

    def reserve_turn(
        self, owner: str, turn: TurnId, admission: int, text: str, *, custody: ExitStack,
    ) -> SingleInputBatch:
        """Enlist rollback before publication; caller retains the original wire scope."""
        from .channel_input_batch import SingleInputBatch

        row = ReservedInput(f"turn:{turn.value}", None, owner, admission, owner, text)

        document = self.reserve_originals(row, custody=custody)
        return SingleInputBatch(document.originals((row.key,)))

    def reserve_originals(
        self, *originals: ReservedInput, custody: ExitStack,
    ) -> InputDocument:
        """Require fresh originals, retaining the exact publication and rollback."""
        document = self.record_originals(*originals, custody=custody)
        if any(document.rows[row.key] is not row for row in originals):
            raise RelationViolationError("Input reservation already exists")
        return document

    def _transition(self, key: str, change) -> bool:
        changed = False

        def update(document: InputDocument) -> InputDocument:
            nonlocal changed
            next_row = change(document.lookup(key))
            if next_row is None:
                return document
            changed = True
            return replace(document, rows={**document.rows, key: next_row})

        self.update_rows((key,), update)
        return changed

    def bind(self, key: str, *, admission: int, turn_id: str, native_id: str, text: str) -> bool:
        return self.bind_originals(
            (key,), admission=admission, turn_id=turn_id, native_id=native_id, text=text
        )

    def bind_turn_input(
        self, keys: tuple[str, ...], *, admission: int, turn_id: str, native_id: str, text: str,
        already_bound: bool,
    ) -> bool:
        """Bind a turn's unresolved inputs to the native input carrying them.

        ``already_bound`` (a send-now re-send) requires the existing binding to match.
        """
        document = self.rows(keys)
        if any(not document.lookup(key).unresolved
               or not document.lookup(key).matches_admission(admission) for key in keys):
            return False
        if already_bound:
            return all(document.lookup(key).matches_native(
                native_id=native_id, turn_id=turn_id, text=text
            ) for key in keys)
        return self.bind_originals(
            keys, admission=admission, turn_id=turn_id, native_id=native_id, text=text
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

        self.update_rows(keys, change)
        return bound

    def started(self, key: str, *, turn_id: str, native_id: str, text: str) -> bool:
        return self._transition(
            key, lambda row: row.started(turn_id=turn_id, native_id=native_id, text=text)
        )

    def settle_unbound(self, keys: tuple[str, ...]) -> InputDocument:
        """Publish one complete retirement and return that exact selected cut."""
        return self.update_rows(keys, lambda document: document.settle_unbound(keys))

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

        self.update_rows(tuple(dict.fromkeys((*keys, *(row.key for row in observed)))), review)

    def dismiss_historical(
        self,
        owners: frozenset[str],
        *,
        awaiting_keys: frozenset[str] | None = None,
    ) -> dict:
        if not owners:
            return InputDocument().delivery_overview(owners, awaiting_keys=awaiting_keys)
        placeholders = ",".join("?" for _ in owners)
        original = self._select(
            f"owner IN ({placeholders}) AND unresolved = 1", tuple(owners)
        )
        changed = {
            row.key: replace(row, notice_dismissed=True)
            for row in original.unknown(owners)
            if row.historical_notice(awaiting_keys) and not row.notice_dismissed
        }
        document = self._publish(
            original,
            replace(original, rows={**original.rows, **changed}) if changed else original,
        )
        return document.delivery_overview(owners, awaiting_keys=awaiting_keys)
