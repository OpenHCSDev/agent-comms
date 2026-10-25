"""Early admission and xdist's later identity, using real pytest sessions and flock."""

from pathlib import Path

import pytest

import pytest_session as sessions
from agent_comms.test_lease import HostTestLease, NewTestRun, TestLeaseBusy

pytest_plugins = ("pytester",)


@pytest.fixture
def controller_session(pytester: pytest.Pytester) -> pytest.Session:
    pytester.makeini("[pytest]\n")
    config = pytester.parseconfig(str(pytester.path))
    return pytest.Session.from_config(config)


def test_controller_acquires_before_imports_and_confirmation_does_not_reacquire(
    tmp_path: Path, controller_session: pytest.Session,
):
    lease = HostTestLease.in_home(tmp_path)
    with sessions.TestSession.from_worker_hint("", NewTestRun(), lease) as owner:
        owner.prepare()
        owner.confirm(controller_session)
        with pytest.raises(TestLeaseBusy, match=lease.refusal):
            with lease.held():
                pass
    with lease.held():
        pass


def test_forged_worker_hint_defers_but_never_bypasses_admission(
    tmp_path: Path, controller_session: pytest.Session,
):
    lease = HostTestLease.in_home(tmp_path)
    with lease.held(), sessions.TestSession.from_worker_hint(
        "not-a-real-worker", NewTestRun(), lease,
    ) as owner:
        owner.prepare()
        with pytest.raises(pytest.UsageError, match=lease.refusal):
            owner.confirm(controller_session)


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
