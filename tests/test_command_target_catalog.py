"""Target families never disguise a broken binding as an unavailable command."""

import pytest

from agent_comms.cli_commands import OneTarget, TargetBatch


@pytest.mark.parametrize("selection,target,resolvable", [
    (OneTarget, "first", True),
    (OneTarget, "ghost", False),
    (TargetBatch, ("first",), True),
    (TargetBatch, ("ghost",), False),
])
def test_each_selection_returns_its_own_result(comms, tmp_path, selection, target, resolvable):
    from agent_comms.cli_commands import StopCliCommand, TargetBatchResult, TargetFailed
    from agent_comms.thread_status import RunningThreadStatus

    comms.registry.declare(comms.registry.new_thread("first", frozenset(), str(tmp_path)), RunningThreadStatus())
    assert type(StopCliCommand.selected_targets(target)) is selection
    if selection is OneTarget and not resolvable:
        with pytest.raises(ValueError):
            StopCliCommand.execute_target(comms, StopCliCommand.selected_targets(target), {})
        return
    result = StopCliCommand.execute_target(comms, StopCliCommand.selected_targets(target), {})
    if selection is OneTarget:
        assert result.stopped == "first"
    else:
        assert type(result) is TargetBatchResult and result.successful is resolvable
        outcome, = result.outcomes
        assert outcome.result.stopped == "first" if resolvable else type(outcome) is TargetFailed
    assert comms.registry.status("first").stopped is resolvable
