"""Target families never disguise a broken binding as an unavailable command."""

import pytest

from agent_comms.cli_commands import OneTarget, TargetBatch
from agent_comms.channel_targets import CommandTarget
from agent_comms.threads import Thread


@pytest.mark.parametrize("selection,target,resolvable", [
    (OneTarget, CommandTarget.named("first"), True),
    (OneTarget, CommandTarget.named("ghost"), False),
    (TargetBatch, (CommandTarget.named("first"),), True),
    (TargetBatch, (CommandTarget.named("ghost"),), False),
])
def test_each_selection_returns_its_own_result(comms, tmp_path, selection, target, resolvable):
    from agent_comms.cli_commands import StopCliCommand, TargetBatchResult, TargetFailed
    from agent_comms.thread_status import RunningThreadStatus

    comms.registry.declare(Thread("first", frozenset(), str(tmp_path)), RunningThreadStatus())
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


def test_batch_capability_owns_selection_catalog_and_execution(comms, tmp_path):
    from agent_comms.cli_commands import ArchiveCliCommand, CliCommand, RestartCliCommand
    from agent_comms.thread_status import ArchivedThreadStatus, StoppedThreadStatus

    names = ("first", "second")
    for name in names:
        comms.registry.declare(Thread(name, frozenset(), str(tmp_path)),
                               StoppedThreadStatus())
    selected = CliCommand.selected_targets(tuple(CommandTarget.named(name) for name in names))
    catalog = CliCommand.target_catalog(comms, selected, project=str(tmp_path))
    assert any(action.declaration is ArchiveCliCommand for action in catalog)
    assert all(action.declaration is not RestartCliCommand for action in catalog)
    with pytest.raises(ValueError, match="single target"):
        RestartCliCommand.execute_target(comms, RestartCliCommand.selected_targets(tuple(CommandTarget.named(name) for name in names)), {})
    assert all(comms.registry.status(name) == StoppedThreadStatus() for name in names)
    result = ArchiveCliCommand.execute_target(
        comms, ArchiveCliCommand.selected_targets(tuple(CommandTarget.named(name) for name in names)), {}, confirmed=True)
    assert len(result.outcomes) == 2 and result.exit_code() == 0
    assert tuple(outcome.target for outcome in result.outcomes) == names
    assert all(comms.registry.status(name) == ArchivedThreadStatus() for name in names)


@pytest.mark.parametrize("presence,allowed", [
    ("running", True), ("idle", True), ("stopped", True),
    ("archived", False), ("deleting", False),
])
def test_channel_pin_filters_hidden_thread_presence(comms, tmp_path, presence, allowed):
    from agent_comms.thread_status import ThreadStatus

    comms.registry.declare(
        Thread("first", frozenset({"team"}), str(tmp_path)),
        ThreadStatus.member_named(presence)(),
    )
    if allowed:
        comms.channels.set_thread_pinned("#team", "first", True)
        assert "first" in comms.channels.catalog.read().pinned_members().get("#team", frozenset())
    else:
        with pytest.raises(ValueError, match="not a member"):
            comms.channels.set_thread_pinned("#team", "first", True)
        assert "first" not in comms.channels.catalog.read().pinned_members().get("#team", frozenset())
