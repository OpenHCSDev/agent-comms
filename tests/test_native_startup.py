"""Native startup's get_state wait grows with the saved session's size and stops at its cap."""


from agent_comms.execution.native_startup import (
    MeasuredSessionLoad,
    NativeStartupPolicy,
    UnmeasuredSessionLoad,
)


def test_readiness_wait_grows_with_session_size_up_to_the_cap():
    policy = NativeStartupPolicy()
    base, step = policy.readiness_seconds, policy.readiness_step_bytes

    def wait(load, base_seconds=base):
        return policy.readiness_timeout(load, base_seconds=base_seconds)

    for small in (UnmeasuredSessionLoad(), MeasuredSessionLoad(0), MeasuredSessionLoad(step)):
        assert wait(small) == base
    assert wait(MeasuredSessionLoad(step + 1)) == base + policy.readiness_step_seconds
    assert wait(MeasuredSessionLoad(1024 * step)) == policy.readiness_max_seconds
    below_cap = MeasuredSessionLoad(3 * step)
    assert wait(below_cap, base_seconds=0.05) == wait(below_cap) - base + 0.05
