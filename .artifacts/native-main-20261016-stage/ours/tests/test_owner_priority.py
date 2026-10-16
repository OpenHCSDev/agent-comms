"""Owners run at background priority so interactive clients win the CPU."""

import os
import subprocess
import sys


def _priority_after_lowering(start: int) -> int:
    script = (
        "import os; from agent_comms.worker import lower_priority; "
        "lower_priority(); print(os.getpriority(os.PRIO_PROCESS, 0))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], check=True, capture_output=True, text=True,
        preexec_fn=lambda: os.setpriority(os.PRIO_PROCESS, 0, start),
    )
    return int(result.stdout)


def test_owner_lowers_its_priority_to_background():
    from agent_comms.worker import OWNER_NICENESS

    assert _priority_after_lowering(0) == OWNER_NICENESS


def test_owner_keeps_a_lower_inherited_priority():
    assert _priority_after_lowering(15) == 15
