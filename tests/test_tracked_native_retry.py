from types import SimpleNamespace

import pytest

from agent_comms.pi_events import NativeRetryEnd
from agent_comms.tracked_turn import (
    FailedTrackedTerminal,
    PendingTrackedTerminal,
    TrackedTurnSession,
)


async def test_recovered_native_request_replaces_prior_refusal_terminal():
    retried = []
    turn = object.__new__(TrackedTurnSession)
    turn.output = SimpleNamespace(request_retried=lambda: retried.append(True))
    turn.terminal = FailedTrackedTerminal("provider refusal")

    await turn.native_retry_finished(NativeRetryEnd(success=True, attempt=1))

    assert retried == [True]
    assert isinstance(turn.terminal, PendingTrackedTerminal)


async def test_exhausted_native_request_keeps_final_refusal_terminal():
    turn = object.__new__(TrackedTurnSession)
    turn.output = SimpleNamespace(request_retried=lambda: pytest.fail("exhausted retry reset"))
    terminal = turn.terminal = FailedTrackedTerminal("final refusal")

    await turn.native_retry_finished(NativeRetryEnd(success=False, attempt=3))

    assert turn.terminal is terminal
