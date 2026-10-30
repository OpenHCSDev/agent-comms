"""Toad and every bus client import the Comms facade and the Todo store, so neither loads the
NominalRefactorAdvisor; only a plan reader's census does.

On 2026-10-30 one import edge (todos -> plan_phases -> phase_evidence -> the advisor) cost
every Toad launch 1.1-1.5 s of the advisor's analysis modules.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.parametrize("module", ["agent_comms.comms", "agent_comms.todos"])
def test_bus_client_never_imports_the_advisor(module: str) -> None:
    loaded = subprocess.run(
        [sys.executable, "-c",
         f"import sys, {module}; print(' '.join(m for m in sys.modules"
         " if m.partition('.')[0] == 'nominal_refactor_advisor'))"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert loaded == []
