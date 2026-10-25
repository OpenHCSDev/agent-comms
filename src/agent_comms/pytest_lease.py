"""Acquire the host lease before pytest imports a checkout's heavy conftest."""

from __future__ import annotations

from contextlib import ExitStack

import pytest
from xdist import is_xdist_worker

from .test_lease import InheritedTestRun, LeaseStart, NewTestRun, TestLease, TestLeaseBusy

_SESSION = pytest.StashKey[ExitStack]()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--integration-lease", type=InheritedTestRun.argument, default=NewTestRun(),
        help="adopt the integrator's kernel-held lease descriptor (not an exemption)",
    )


def ensure_test_session(config: pytest.Config, start: LeaseStart = NewTestRun()) -> None:
    """The pytest config owns one lease lifetime; xdist workers belong to it."""
    if is_xdist_worker(config):
        return
    try:
        config.stash[_SESSION]
    except KeyError:
        lifetime = ExitStack()
        try:
            lifetime.enter_context(TestLease.current().held(start))
        except TestLeaseBusy as error:
            lifetime.close()
            raise pytest.UsageError(str(error)) from error
        config.add_cleanup(lifetime.close)
        config.stash[_SESSION] = lifetime


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(
    early_config: pytest.Config, parser: pytest.Parser, args: list[str],
) -> None:
    # pytest's early namespace is its argument boundary, before final Config.option.
    start: LeaseStart = parser.parse_known_args(args).integration_lease
    ensure_test_session(early_config, start)
