"""Durable ACP input attempts and its transport cursor.

An UNKNOWN row is never a request to retry. It records that a model input
may have reached Pi. Only Pi's matching native user start can change it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .declarations import (
    Message,
    RelationViolationError,
    ResponsePolicy,
    Thread,
    _atomic_write_text,
    _store_lock,
)

_NATIVE_ID = re.compile(r"[0-9a-f]{32}\Z")


class InputDispositions:
    def __init__(self, root: Path) -> None:
        self.path = root / "input_dispositions.json"

    @staticmethod
    def bus_key(message: Message, owner: Thread) -> str:
        """A channel sequence has one attempt per stable recipient incarnation."""
        suffix = (
            ""
            if message.response_policy is ResponsePolicy.DIRECT
            else f":owner:{float(owner.created_at).hex()}"
        )
        return f"bus:{message.seq}{suffix}"

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if type(value) is not dict or value.get("version") != 1:
            raise RelationViolationError("Invalid ACP input disposition ledger")
        rows = value.get("rows")
        if type(rows) is not dict or any(
            type(key) is not str
            or not key
            or type(row) is not dict
            or row.get("key") != key
            or row.get("status") not in {"unknown", "started"}
            or type(row.get("owner")) is not str
            or type(row.get("admission")) is not int
            or type(row.get("target")) is not str
            or type(row.get("source_text")) is not str
            or (row.get("sequence") is not None and type(row.get("sequence")) is not int)
            or (row.get("turn_id") is not None and type(row.get("turn_id")) is not str)
            or (
                row.get("native_id") is not None
                and (
                    type(row.get("native_id")) is not str
                    or _NATIVE_ID.fullmatch(row["native_id"]) is None
                )
            )
            or (row.get("sent_text") is not None and type(row.get("sent_text")) is not str)
            for key, row in rows.items()
        ):
            raise RelationViolationError("Invalid ACP input disposition rows")
        return rows

    def _write(self, rows: dict[str, dict[str, Any]]) -> None:
        _atomic_write_text(
            self.path, json.dumps({"version": 1, "rows": rows}, sort_keys=True), fsync_parent=True
        )

    def record(
        self,
        key: str,
        *,
        seq: int | None,
        owner: str,
        admission: int,
        target: str,
        text: str,
    ) -> bool:
        """Persist UNKNOWN before cursor advance or any Pi prompt write."""
        if not key or not owner or not target or not text or admission <= 0:
            raise ValueError("Invalid ACP input identity")
        if seq is not None and (
            seq <= 0 or (key != f"bus:{seq}" and not key.startswith(f"bus:{seq}:owner:"))
        ):
            raise ValueError("Bus input key and sequence disagree")
        with _store_lock(self.path):
            rows = self._read()
            if key in rows:
                return False
            rows[key] = {
                "key": key,
                "sequence": seq,
                "owner": owner,
                "admission": admission,
                "target": target,
                "source_text": text,
                "turn_id": None,
                "native_id": None,
                "sent_text": None,
                "status": "unknown",
            }
            self._write(rows)
            return True

    def bind(self, key: str, *, admission: int, turn_id: str, native_id: str, text: str) -> bool:
        """Bind a single UNKNOWN attempt to Pi's private ID before its send."""
        if not turn_id or _NATIVE_ID.fullmatch(native_id) is None:
            raise ValueError("Invalid native input attempt")
        with _store_lock(self.path):
            rows = self._read()
            row = rows.get(key)
            if (
                row is None
                or row["status"] != "unknown"
                or row["admission"] != admission
                or row["native_id"] is not None
            ):
                return False
            row["turn_id"] = turn_id
            row["native_id"] = native_id
            row["sent_text"] = text
            self._write(rows)
            return True

    def started(self, key: str, *, turn_id: str, native_id: str, text: str) -> bool:
        """CAS UNKNOWN to STARTED only for the bound native user start."""
        with _store_lock(self.path):
            rows = self._read()
            row = rows.get(key)
            if row is None or any(
                (
                    row["status"] != "unknown",
                    row["turn_id"] != turn_id,
                    row["native_id"] != native_id,
                    row["sent_text"] != text,
                )
            ):
                return False
            row["status"] = "started"
            self._write(rows)
            return True

    def review_for_goal(
        self,
        keys: tuple[str, ...],
        *,
        owners: frozenset[str],
        goal_id: str,
        goal_revision: int,
        turn_id: str,
    ) -> None:
        """Record an explicit wait decision, without claiming native start or replay."""
        with _store_lock(self.path):
            rows = self._read()
            if any(
                key not in rows
                or rows[key]["owner"] not in owners
                or rows[key]["status"] != "unknown"
                for key in keys
            ):
                raise ValueError("Reviewed inputs changed; inspect them again.")
            for key in keys:
                rows[key].setdefault("goal_reviews", {})[goal_id] = {
                    "goal_revision": goal_revision,
                    "turn_id": turn_id,
                }
            if keys:
                self._write(rows)

    @staticmethod
    def reviewed_for_goal(row: dict[str, Any], goal_id: str) -> bool:
        return goal_id in row.get("goal_reviews", {})

    def status(self, key: str) -> str | None:
        with _store_lock(self.path):
            row = self._read().get(key)
            return row["status"] if row is not None else None

    def get(self, key: str) -> dict[str, Any] | None:
        with _store_lock(self.path):
            row = self._read().get(key)
            return dict(row) if row is not None else None

    @staticmethod
    def public(row: dict[str, Any]) -> dict[str, Any]:
        """Public delivery projection; native receipt authority stays private."""
        return {
            "inputId": row["key"].removeprefix("acp:"),
            "sequence": row["sequence"],
            "target": row["target"],
            "text": row["source_text"],
            "status": row["status"],
            **(
                {"reviewedForGoals": sorted(row["goal_reviews"])} if row.get("goal_reviews") else {}
            ),
        }

    def unknown(self, owners: frozenset[str]) -> list[dict[str, Any]]:
        with _store_lock(self.path):
            rows = (
                dict(row)
                for row in self._read().values()
                if row["owner"] in owners and row["status"] == "unknown"
            )
            return sorted(rows, key=lambda row: (row["sequence"] is None, row["sequence"] or 0))


class AcpDeliveryCursors:
    """ACP scheduling position, independent of human/UI read markers."""

    def __init__(self, root: Path) -> None:
        self.path = root / "acp_delivery_cursors.json"

    def _read(self) -> dict[str, dict[str, int]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if type(value) is not dict or value.get("version") != 1:
            raise RelationViolationError("Invalid ACP delivery cursor ledger")
        rows = value.get("rows")
        if type(rows) is not dict or any(
            type(name) is not str
            or not name
            or type(row) is not dict
            or type(row.get("cursor")) is not int
            or type(row.get("legacy_through")) is not int
            or row["cursor"] < 0
            or row["legacy_through"] < 0
            for name, row in rows.items()
        ):
            raise RelationViolationError("Invalid ACP delivery cursor rows")
        return rows

    def _write(self, rows: dict[str, dict[str, int]]) -> None:
        _atomic_write_text(
            self.path, json.dumps({"version": 1, "rows": rows}, sort_keys=True), fsync_parent=True
        )

    def initialize(
        self, aliases: frozenset[str], owner: str, *, high_water: int, fresh: bool
    ) -> tuple[int, int]:
        with _store_lock(self.path):
            rows = self._read()
            matches = [name for name in rows if name in aliases]
            if len(matches) > 1:
                raise RelationViolationError("Ambiguous ACP delivery cursor after rename")
            if matches:
                row = rows[matches[0]]
            else:
                if high_water < 0:
                    raise ValueError("Invalid bus high water")
                row = {
                    "cursor": high_water if fresh else 0,
                    "legacy_through": 0 if fresh else high_water,
                }
                rows[owner] = row
                self._write(rows)
            return row["cursor"], row["legacy_through"]

    def advance(self, aliases: frozenset[str], through: int) -> None:
        if through < 0:
            raise ValueError("Invalid ACP delivery cursor")
        with _store_lock(self.path):
            rows = self._read()
            matches = [name for name in rows if name in aliases]
            if len(matches) != 1:
                raise RelationViolationError("Missing or ambiguous ACP delivery cursor")
            row = rows[matches[0]]
            if through > row["cursor"]:
                row["cursor"] = through
                self._write(rows)

    def cursor(self, aliases: frozenset[str]) -> int:
        """Read the scheduling boundary without creating or advancing it."""
        with _store_lock(self.path):
            rows = [row for name, row in self._read().items() if name in aliases]
            if len(rows) > 1:
                raise RelationViolationError("Ambiguous ACP delivery cursor after rename")
            return rows[0]["cursor"] if rows else 0
