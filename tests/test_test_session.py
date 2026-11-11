"""Controller setup and xdist identity, using real pytest sessions and flock."""

import subprocess
import sys
import threading
import os
from pathlib import Path

import pytest

import pytest_session as sessions
from agent_comms.execution.test_lease import HostTestLease, InheritedTestRun, NewTestRun
from agent_comms.projects.storage_rules import StorageRule

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
        assert owner.start.descriptors()
        with lease.held(InheritedTestRun(os.dup(owner.start.descriptors()[0]))):
            with lease.observed() as shared:
                assert shared.held
        with lease.observed() as claim:
            assert claim.held
    with lease.observed() as claim:
        assert not claim.held


def test_forged_worker_hint_defers_but_never_bypasses_admission(
    tmp_path: Path, controller_session: pytest.Session,
):
    lease = HostTestLease.in_home(tmp_path)
    hold = ("from pathlib import Path; import sys; from agent_comms.execution.test_lease import"
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


def test_running_factory_uses_the_configured_directory(
    tmp_path_factory: pytest.TempPathFactory, pytestconfig: pytest.Config,
) -> None:
    assert tmp_path_factory.getbasetemp() == Path(pytestconfig.getoption("basetemp")).resolve()


def test_default_temp_is_fresh_and_keeps_other_run_data(
    tmp_path: Path, pytester: pytest.Pytester,
) -> None:
    pytester.makeini("[pytest]\n")
    previous = tmp_path / "previous-run"
    previous.mkdir()
    retained = previous / "evidence"
    retained.write_text("retained")
    # pytester.parseconfig supplies its own basetemp. Use pytest's real public
    # constructor here so the absent-option input actually remains absent.
    configurations = tuple(pytest.Config.fromdictargs({}, [str(pytester.path)]) for _ in range(2))
    allocated: list[Path] = []
    try:
        for config in configurations:
            lease = HostTestLease.in_home(tmp_path)
            with sessions.TestSession.from_worker_hint("", NewTestRun(), lease) as owner:
                owner.configure(config)
            into = Path(config.getoption("basetemp"))
            allocated.append(into)
            assert into.parent.parent.resolve() == StorageRule().scratch_base.resolve()
            assert not into.exists()
            factory = pytest.TempPathFactory.from_config(config, _ispytest=True)
            assert factory.getbasetemp() == into.resolve()
            assert retained.read_text() == "retained"
        assert allocated[0].parent != allocated[1].parent
    finally:
        for config in configurations:
            config._ensure_unconfigure()
        # Only these empty directories were allocated by this control. Never
        # recursively clean another run or pytest's old shared garbage directory.
        for into in allocated:
            if into.exists():
                into.rmdir()
            into.parent.rmdir()


@pytest.mark.parametrize("worker_hint", ("", "gw0"), ids=("controller", "worker"))
def test_supplied_temp_keeps_its_path_and_existing_data(
    tmp_path: Path, pytester: pytest.Pytester, worker_hint: str,
) -> None:
    pytester.makeini("[pytest]\n")
    supplied = tmp_path / "supplied"
    supplied.mkdir()
    retained = supplied / "evidence"
    retained.write_text("retained")
    config = pytester.parseconfig(str(pytester.path), "--basetemp", str(supplied))
    lease = HostTestLease.in_home(tmp_path)
    with sessions.TestSession.from_worker_hint(worker_hint, NewTestRun(), lease) as owner:
        owner.configure(config)
    assert Path(config.getoption("basetemp")) == supplied
    assert retained.read_text() == "retained"
