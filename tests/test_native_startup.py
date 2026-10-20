"""Native startup readiness is size-aware, and cancellation reaps a starting child."""


import pytest

from agent_comms.native_startup import MeasuredSessionLoad, NativeStartupPolicy, UnmeasuredSessionLoad

pytestmark = pytest.mark.usefixtures("native_rpc_fixture")


def test_readiness_budget_is_size_aware_bounded_and_not_a_turn_deadline():
    policy = NativeStartupPolicy()
    mib = 1024 * 1024
    for load, expected in (
        (UnmeasuredSessionLoad(), 10.0),
        (MeasuredSessionLoad(0), 10.0),
        (MeasuredSessionLoad(8 * mib), 10.0),
        (MeasuredSessionLoad(8 * mib + 1), 12.0),
        (MeasuredSessionLoad(111_533_315), 35.0),
        (MeasuredSessionLoad(112_851_639), 35.0),
        (MeasuredSessionLoad(1024 * mib), 35.0),
    ):
        assert policy.readiness_timeout(load, base_seconds=policy.readiness_seconds) == expected
    assert policy.readiness_timeout(MeasuredSessionLoad(112_851_639), base_seconds=0.05) == 26.05


