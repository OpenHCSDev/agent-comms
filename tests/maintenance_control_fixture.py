"""Disposable test-only phase writer. NEVER import from production code.

An installed agent-comms package deliberately exports no operator mutation
surface. The real control plane needs OS-separated authority and old-client
exclusion; this fixture exercises only new-code admission on private roots.
"""

from __future__ import annotations

import json
from uuid import uuid4

from agent_comms.errors import RelationViolationError
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceReceipt
from agent_comms.store_files import _atomic_write_text, _store_lock


class FixtureMaintenanceControl:
    def __init__(self, barrier: MaintenanceBarrier):
        self.barrier = barrier

    def _write_unlocked(self, receipt: MaintenanceReceipt) -> None:
        gate = self.barrier
        root = str(gate.registry_path.parent.resolve())
        marker = {"version": 1, "root": root, "generation": receipt.generation}
        state = {
            **marker,
            "operator": receipt.operator,
            "nonce": receipt.nonce,
            "phase": receipt.phase,
        }
        # Marker first: a failed state write leaves admission closed.
        _atomic_write_text(gate.marker_path, json.dumps(marker, sort_keys=True), fsync_parent=True)
        _atomic_write_text(gate.state_path, json.dumps(state, sort_keys=True), fsync_parent=True)

    def begin(self, operator: str = "disposable-fixture") -> MaintenanceReceipt:
        if type(operator) is not str or not operator or len(operator) > 128:
            raise ValueError("Fixture operator must have bounded identity")
        gate = self.barrier
        with _store_lock(gate.wire_path), _store_lock(gate.registry_path):
            current = gate.current_unlocked()
            if current is not None and current.phase != "ready":
                raise RelationViolationError("Maintenance already active or uncertain")
            generation = current.generation + 1 if current is not None else 1
            receipt = MaintenanceReceipt(generation, operator, uuid4().hex, "draining")
            self._write_unlocked(receipt)
            return receipt

    def advance(self, expected: MaintenanceReceipt, phase: str) -> MaintenanceReceipt:
        transitions = {"draining": "paused", "paused": "installing"}
        if type(expected) is not MaintenanceReceipt or transitions.get(expected.phase) != phase:
            raise ValueError("Maintenance transition requires a closed expected phase")
        gate = self.barrier
        with _store_lock(gate.wire_path), _store_lock(gate.registry_path):
            if gate.current_unlocked() != expected:
                raise RelationViolationError("Maintenance epoch/operator changed")
            receipt = MaintenanceReceipt(
                expected.generation + 1, expected.operator, uuid4().hex, phase
            )
            self._write_unlocked(receipt)
            return receipt
