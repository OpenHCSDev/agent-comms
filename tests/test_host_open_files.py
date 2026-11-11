"""The host takes the open-file budget its own kernel allows, instead of a shell's default."""

from __future__ import annotations

import resource
import subprocess
import sys

from agent_comms.execution.worker import open_every_file_this_host_may_need

TAKES_ITS_BUDGET = """
import resource
from agent_comms.execution.worker import open_every_file_this_host_may_need

soft_before, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (min(1024, hard), hard))
open_every_file_this_host_may_need()
soft_after, hard_after = resource.getrlimit(resource.RLIMIT_NOFILE)
print(soft_after, hard_after)
"""


def test_the_host_raises_its_soft_limit_to_the_hard_limit() -> None:
    """A child starts at the inherited soft limit and ends holding its whole allowance.

    This runs in its own interpreter because the limit is process state: raising it in
    the test runner would leave every later test measuring a budget this one set.
    """
    result = subprocess.run(
        [sys.executable, "-c", TAKES_ITS_BUDGET],
        capture_output=True, text=True, check=True,
    )
    soft, hard = (int(value) for value in result.stdout.split())
    assert soft == hard, "the host kept the soft limit a login shell handed it"
    assert soft > 1024, "a host serving every session needs more than the shell default"


def test_taking_the_budget_twice_changes_nothing() -> None:
    """The second call has nothing left to raise, so it must not lower the first."""
    before = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        open_every_file_this_host_may_need()
        open_every_file_this_host_may_need()
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        assert soft == hard
        assert hard == before[1], "the hard limit is the kernel's to lower, not this host's"
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, before)
