"""One pytest controller lifetime, configured before test setup or execution."""

from __future__ import annotations

import os
from contextlib import ExitStack

import pytest
from xdist import is_xdist_worker

from agent_comms.test_lease import (
    InheritedTestRun,
    LeaseStart,
    TestLease,
    TestLeaseBusy,
)


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
        try:
            self.enter_context(self.lease.held(self.start))
        except TestLeaseBusy as error:
            self.close()
            raise pytest.UsageError(str(error)) from error

    def prepare(self) -> None:
        self.acquire()

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
    owner.prepare()


@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session: pytest.Session) -> None:
    session.config.stash[_SESSION].confirm(session)
