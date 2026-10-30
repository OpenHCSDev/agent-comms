"""Controller setup and xdist identity, using real pytest sessions and flock."""

import subprocess
import sys
import threading
from pathlib import Path

import pytest

import pytest_session as sessions
from agent_comms.test_lease import HostTestLease, NewTestRun

pytest_plugins = ("pytester",)


@pytest.fixture
def controller_session(pytester: pytest.Pytester) -> pytest.Session:
    pytester.makeini("[pytest]\n")
    config = pytester.parseconfig(str(pytester.path))
    return pytest.Session.from_config(config)


def test_controller_prepares_once_and_confirmation_does_not_reacquire(
    tmp_path: Path, controller_session: pytest.Session,
):
    lease = HostTestLease.in_home(tmp_path)
    with sessions.TestSession.from_worker_hint("", NewTestRun(), lease) as owner:
        owner.prepare()
        owner.confirm(controller_session)
        with lease.observed() as claim:
            assert claim.held
    with lease.observed() as claim:
        assert not claim.held


def test_forged_worker_hint_defers_but_never_bypasses_admission(
    tmp_path: Path, controller_session: pytest.Session,
):
    lease = HostTestLease.in_home(tmp_path)
    hold = ("from pathlib import Path; import sys; from agent_comms.test_lease import"
            " HostTestLease\nwith HostTestLease(Path(sys.argv[1])).held():\n"
            "    print('held', flush=True); sys.stdin.read(1)")
    with subprocess.Popen((sys.executable, "-c", hold, str(lease.path)), stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, text=True) as holder, \
            sessions.TestSession.from_worker_hint(
                "not-a-real-worker", NewTestRun(), lease) as owner:
        assert holder.stdout.readline().strip() == "held"
        owner.prepare()
        confirming = threading.Thread(target=owner.confirm, args=(controller_session,))
        confirming.start()
        confirming.join(timeout=1)
        assert confirming.is_alive()
        holder.communicate(input="x", timeout=5)
        confirming.join(timeout=5)
        assert not confirming.is_alive()
        with lease.observed() as claim:
            assert claim.held


def test_confirmed_foreign_worker_belongs_to_its_controller(
    tmp_path: Path, controller_session: pytest.Session, monkeypatch: pytest.MonkeyPatch,
):
    # xdist owns this foreign Config extension. The hint alone above is insufficient.
    monkeypatch.setattr(controller_session.config, "workerinput", {"workerid": "gw0"},
                        raising=False)
    lease = HostTestLease.in_home(tmp_path)
    with lease.held(), sessions.TestSession.from_worker_hint(
        "gw0", NewTestRun(), lease,
    ) as owner:
        owner.prepare()
        owner.confirm(controller_session)
