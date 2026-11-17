"""One pytest controller lifetime, configured before test setup or execution."""

from __future__ import annotations

import os
from contextlib import ExitStack

import pytest
from xdist import is_xdist_worker

from agent_comms.execution.test_lease import (
    InheritedTestRun,
    LeaseStart,
    TestLease,
)
from agent_comms.projects.storage_rules import StorageRule


class TestSession(ExitStack):
    """The controller holds the resource before test setup or execution."""

    def __init__(self, start: LeaseStart, lease: TestLease) -> None:
        super().__init__()
        self.start = start
        self.lease = lease

    @classmethod
    def from_worker_hint(cls, hint: str, start: LeaseStart, lease: TestLease) -> TestSession:
        # xdist publishes this hint before _prepareconfig, but only sets the real
        # Config.workerinput afterwards. It can defer the claim, never exempt it.
        if hint:
            return DeferredWorkerCheck(start, lease)
        return cls(start, lease)

    def acquire(self) -> None:
        self.start = self.enter_context(self.lease.held(self.start))

    def prepare(self) -> None:
        self.acquire()

    def configure(self, config: pytest.Config) -> None:
        self.prepare()
        # This option is foreign pytest input. Supplied/controller-worker paths
        # keep pytest's explicit-basetemp contract; only its absent default is ours.
        if config.option.basetemp is None:
            # pytest's own temp root numbers, locks and prunes each run (keeping
            # tmp_path_retention_count); an explicit basetemp would turn that off and
            # leave every run's tree behind. Only the root's location is ours.
            scratch = StorageRule().scratch_base
            scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.environ["PYTEST_DEBUG_TEMPROOT"] = str(scratch)

    def confirm(self, session: pytest.Session) -> None:
        pass


class DeferredWorkerCheck(TestSession):
    def prepare(self) -> None:
        pass

    def confirm(self, session: pytest.Session) -> None:
        if not is_xdist_worker(session):
            self.acquire()


_SESSION = pytest.StashKey[TestSession]()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        InheritedTestRun.option,
        type=InheritedTestRun.argument,
        default=TestLease.default_start,
        help="adopt the integrator's kernel-held descriptor (not an exemption)",
    )


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    start: LeaseStart = config.getoption("integration_lease")
    owner = TestSession.from_worker_hint(
        os.environ.get("PYTEST_XDIST_WORKER", ""),
        start,
        TestLease.current(),
    )
    config.stash[_SESSION] = owner
    config.add_cleanup(owner.close)
    owner.configure(config)


@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session: pytest.Session) -> None:
    session.config.stash[_SESSION].confirm(session)


@pytest.fixture
def integration_lease(pytestconfig: pytest.Config) -> LeaseStart:
    """The controller lends its existing kernel claim to a nested test runner."""
    return pytestconfig.stash[_SESSION].start
