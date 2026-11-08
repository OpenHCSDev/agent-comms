"""Phase declarations stay usable before action and storage modules are loaded."""

from pathlib import Path
import subprocess
import sys

import pytest

from agent_comms.planning.orders import UnorderedProblemOrder


@pytest.mark.parametrize("module", ("planning.plan_phases", "work_dispatch", "planning.goal_actions", "planning.orders"))
def test_import_order_does_not_reintroduce_dispatch_cycle(module):
    checkout = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        (sys.executable, "-c", f"import agent_comms.{module}"),
        cwd=checkout,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_unordered_problem_cannot_start_checked_work():
    with pytest.raises(ValueError):
        UnorderedProblemOrder().reissued(1)
