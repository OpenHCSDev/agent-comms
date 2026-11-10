"""Stored phases resolve from a cold Todo import, not a caller's import side effects."""

import subprocess
import sys
from pathlib import Path

from agent_comms.planning.order_phase import UnplannedPhase
from agent_comms.planning.plan_phases import RetainedStorePhase


def test_stored_order_schema_and_phase_decode_acquire_their_declarations() -> None:
    fixture = Path(__file__).with_name("cold_order_phase_fixture.py")
    result = subprocess.run(
        (sys.executable, str(fixture), RetainedStorePhase.declared_name, UnplannedPhase.declared_name),
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
