"""Default-off, wire-root maintenance admission for *new-code* callers only.

An enabled record does not constrain a process that imported older code. External
old-client exclusion and a Toad ingress pause are required before live use.
The enabled witness is written before the state and never removed; a lost or
corrupt state therefore denies admission instead of silently returning to OFF.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .declarations import (
    RelationViolationError,
    _atomic_write_text,
    _store_lock,
    unique_wire_object,
)


@dataclass(frozen=True)
class MaintenanceReceipt:
    generation: int
    operator: str
    nonce: str
    phase: str


class MaintenanceBarrier:
    """One root's persistent CAS gate; the caller owns its admission locks."""

    def __init__(self, registry_path: Path):
        self.registry_path = Path(registry_path)
        self.wire_path = self.registry_path.parent / "wire"
        self.marker_path = self.registry_path.parent / ".maintenance-enabled"
        self.state_path = self.registry_path.parent / ".maintenance-state"

    @staticmethod
    def _read(path: Path) -> dict[str, object] | None:
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        except OSError as error:
            raise RelationViolationError("Maintenance witness is inaccessible") from error
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or (os.name == "posix" and info.st_uid != os.geteuid())
                or (os.name == "posix" and stat.S_IMODE(info.st_mode) != 0o600)
                or info.st_nlink != 1
                or info.st_size > 4096
            ):
                raise RelationViolationError("Maintenance witness is not private and regular")
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = -1
                value = json.load(stream, object_pairs_hook=unique_wire_object)
        except (OSError, UnicodeError, ValueError) as error:
            raise RelationViolationError("Maintenance witness is invalid") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if type(value) is not dict:
            raise RelationViolationError("Maintenance witness is malformed")
        return value

    def current_unlocked(self) -> MaintenanceReceipt | None:
        """Call while holding the registry lock OR the wire lock.

        For a send, the wire lock stays held through stdin.write; for a claim,
        the registry lock spans the turn's durable transition. A phase change
        takes both locks in wire -> registry order.
        """
        marker = self._read(self.marker_path)
        state = self._read(self.state_path)
        if marker is None and state is None:
            return None  # Only fresh, never-enabled roots are default OFF.
        if marker is None or state is None:
            raise RelationViolationError("Maintenance witness incomplete; admission closed")
        if set(marker) != {"version", "root", "generation"} or set(state) != {
            "version",
            "root",
            "generation",
            "operator",
            "nonce",
            "phase",
        }:
            raise RelationViolationError("Maintenance witness fields are invalid")
        root = str(self.registry_path.parent.resolve())
        generation = marker["generation"]
        if (
            type(marker["version"]) is not int
            or marker["version"] != 1
            or type(state["version"]) is not int
            or state["version"] != 1
            or marker["root"] != root
            or state["root"] != root
            or type(generation) is not int
            or not 0 < generation < 1 << 63
            or state["generation"] != generation
            or type(state["operator"]) is not str
            or not state["operator"]
            or type(state["nonce"]) is not str
            or len(state["nonce"]) != 32
            or state["phase"] not in ("draining", "paused", "installing", "ready")
        ):
            raise RelationViolationError("Maintenance witness inconsistent; admission closed")
        return MaintenanceReceipt(generation, state["operator"], state["nonce"], state["phase"])

    def assert_open_unlocked(self) -> None:
        receipt = self.current_unlocked()
        if receipt is not None and receipt.phase != "ready":
            raise RelationViolationError("Maintenance admission closed")

    def read(self) -> MaintenanceReceipt | None:
        with _store_lock(self.wire_path):
            return self.current_unlocked()

    @contextmanager
    def admit_ingress(self) -> Iterator[MaintenanceReceipt | None]:
        """Read and hold the wire admission lock through a *synchronous* spawn.

        Do not await while inside this context. A child still needs to perform
        its own claim/send checks, and old-import children are not protected.
        """
        with _store_lock(self.wire_path):
            self.assert_open_unlocked()
            yield self.current_unlocked()

    def _write_unlocked(self, receipt: MaintenanceReceipt) -> None:
        root = str(self.registry_path.parent.resolve())
        marker = {"version": 1, "root": root, "generation": receipt.generation}
        state = {
            **marker,
            "operator": receipt.operator,
            "nonce": receipt.nonce,
            "phase": receipt.phase,
        }
        # Marker first: crash or fsync UNKNOWN leaves mismatched witnesses CLOSED.
        _atomic_write_text(self.marker_path, json.dumps(marker, sort_keys=True), fsync_parent=True)
        _atomic_write_text(self.state_path, json.dumps(state, sort_keys=True), fsync_parent=True)

    def begin(self, operator: str) -> MaintenanceReceipt:
        """Explicit control-plane transition. Never called by normal traffic.

        This is not an OS operator ACL or proof that old imports are excluded.
        A live controller must supply those controls before invoking begin.
        """
        if type(operator) is not str or not operator or len(operator) > 128:
            raise ValueError("Maintenance operator must have bounded identity")
        with _store_lock(self.wire_path), _store_lock(self.registry_path):
            current = self.current_unlocked()
            if current is not None and current.phase != "ready":
                raise RelationViolationError("Maintenance already active or uncertain")
            generation = current.generation + 1 if current is not None else 1
            receipt = MaintenanceReceipt(generation, operator, uuid4().hex, "draining")
            self._write_unlocked(receipt)
            return receipt

    def advance(self, expected: MaintenanceReceipt, phase: str) -> MaintenanceReceipt:
        """CAS closed phases; reopening is intentionally unsupported until an
        independently verified old-client exclusion/release capability exists.
        """
        transitions = {"draining": "paused", "paused": "installing"}
        if type(expected) is not MaintenanceReceipt or transitions.get(expected.phase) != phase:
            raise ValueError("Maintenance transition requires a closed expected phase")
        with _store_lock(self.wire_path), _store_lock(self.registry_path):
            if self.current_unlocked() != expected:
                raise RelationViolationError("Maintenance epoch/operator changed")
            receipt = MaintenanceReceipt(
                expected.generation + 1, expected.operator, uuid4().hex, phase
            )
            self._write_unlocked(receipt)
            return receipt
