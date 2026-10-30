"""Process exclusion and declaration-selected CI admission, not mocked flock."""

import os
from pathlib import Path
import pwd
import subprocess
import sys

import pytest

from agent_comms import test_lease as leases


_HOLD = """
from pathlib import Path
import sys
from agent_comms.test_lease import HostTestLease
with HostTestLease(Path(sys.argv[1])).held():
    print('held', flush=True)
    sys.stdin.read(1)
"""

_ADOPT = """
from pathlib import Path
import sys
from agent_comms.test_lease import HostTestLease, InheritedTestRun
with HostTestLease(Path(sys.argv[1])).held(InheritedTestRun(int(sys.argv[2]))):
    print('adopted')
"""


def test_second_process_waits_for_the_holder_then_runs(tmp_path: Path) -> None:
    lease = leases.HostTestLease.in_home(tmp_path)
    command = (sys.executable, "-c", _HOLD, str(lease.path))
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          text=True) as holder:
        assert holder.stdout.readline().strip() == "held"
        with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              text=True) as queued:
            with pytest.raises(subprocess.TimeoutExpired):
                queued.wait(timeout=1)
            holder.communicate(input="x", timeout=5)
            assert queued.stdout.readline().strip() == "held"
            queued.communicate(input="x", timeout=5)
    assert queued.returncode == 0


def test_child_adopts_the_same_claim_and_parent_keeps_it(tmp_path: Path) -> None:
    lease = leases.HostTestLease.in_home(tmp_path)
    with lease.held() as start:
        adopted = subprocess.run(
            (sys.executable, "-c", _ADOPT, str(lease.path),
             *(str(descriptor) for descriptor in start.descriptors())),
            pass_fds=start.descriptors(), capture_output=True, text=True, timeout=5,
        )
        assert adopted.returncode == 0, adopted.stderr
        assert adopted.stdout.strip() == "adopted"


def test_foreign_descriptor_cannot_exempt_a_controller(tmp_path: Path) -> None:
    lease = leases.HostTestLease.in_home(tmp_path)
    lease.path.parent.mkdir(parents=True)
    lease.path.touch()
    with (tmp_path / "foreign").open("w") as foreign:
        with pytest.raises(ValueError, match="is not the claim"):
            with lease.held(leases.InheritedTestRun(foreign.fileno())):
                pass


def test_only_declared_ci_runner_avoids_the_host_claim(tmp_path: Path) -> None:
    ci = leases.TestLease.for_runner_programs(
        (Path(leases.GithubActionsTestLease.runner_program),), tmp_path,
    )
    with ci.held(), ci.held():
        pass
    local = leases.TestLease.for_runner_programs((Path(sys.executable),), tmp_path)
    assert local.path == leases.HostTestLease.in_home(tmp_path).path


def test_environment_does_not_choose_ci_or_move_the_host_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = leases.TestLease.current().path
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("HOME", str(tmp_path))
    current = leases.TestLease.current()
    assert current.path == original
    assert current.path == leases.HostTestLease.in_home(
        Path(pwd.getpwuid(os.getuid()).pw_dir),
    ).path
