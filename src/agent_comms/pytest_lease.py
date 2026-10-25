"""Acquire the host lease before pytest imports a checkout's heavy conftest."""

from __future__ import annotations

from contextlib import ExitStack

import pytest
from xdist import is_xdist_worker

from .test_lease import InheritedTestRun, LeaseStart, NewTestRun, TestLease, TestLeaseBusy

def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--integration-lease", type=InheritedTestRun.argument, default=NewTestRun(),
        help="adopt the integrator's kernel-held lease descriptor (not an exemption)",
    )


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(
    early_config: pytest.Config, parser: pytest.Parser, args: list[str],
) -> None:
    """The controller's Config owns the lifetime, before application imports."""
    if is_xdist_worker(early_config):
        return
    # pytest's early namespace is its argument boundary, before final Config.option.
    start: LeaseStart = parser.parse_known_args(args).integration_lease
    lifetime = ExitStack()
    try:
        lifetime.enter_context(TestLease.current().held(start))
    except TestLeaseBusy as error:
        lifetime.close()
        raise pytest.UsageError(str(error)) from error
    early_config.add_cleanup(lifetime.close)
