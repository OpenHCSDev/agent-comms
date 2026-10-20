"""Target families never disguise a broken binding as an unavailable command."""

import pytest

from agent_comms.channel_targets import ChannelTarget, ProjectTarget, ThreadTarget
from agent_comms.cli_commands import CliCommand, OneTarget, ReadTargetCliCommand, TargetBatch
from agent_comms.thread_status import StoppedThreadStatus


@pytest.mark.parametrize("target_type,binding", [
    (ThreadTarget, "thread_bindings"),
    (ChannelTarget, "channel_bindings"),
    (ProjectTarget, "project_bindings"),
])
def test_catalog_binding_failure_propagates_for_each_target_family(
    comms, tmp_path, monkeypatch, target_type, binding,
):
    for name in ("selected", "other"):
        comms.registry.declare(
            comms.registry.new_thread(name, frozenset({'team'}), str(tmp_path)), StoppedThreadStatus(),
        )
    target = target_type(
        str(tmp_path) if target_type is ProjectTarget else
        "#team" if target_type is ChannelTarget else "selected"
    )

    def broken(cls, *args, **kwargs):
        raise ValueError("broken binding")

    monkeypatch.setattr(ReadTargetCliCommand, binding, classmethod(broken))
    for selection in (target, (target, ThreadTarget("other"))):
        with pytest.raises(ValueError, match="broken binding"):
            CliCommand.target_catalog(comms, CliCommand.selected_targets(selection), project=str(tmp_path))


def test_channel_catalog_resolves_members_once_per_selected_target(comms, tmp_path, monkeypatch):
    from agent_comms.channels import AllOfMatch, SavedView, ViewKind, ViewPredicate
    from agent_comms.cli_commands import StartCliCommand, StopCliCommand
    from agent_comms.registry_document import RegistrySnapshot

    for name, tags in (("first", {"team", "other"}), ("second", {"team"})):
        comms.registry.declare(
            comms.registry.new_thread(name, frozenset(tags), str(tmp_path)), StoppedThreadStatus())
    comms.channels.set_saved_view(SavedView(
        "both", ViewKind.PARTICIPANTS, ViewPredicate(AllOfMatch, frozenset({"team", "other"}))))
    original = RegistrySnapshot.visible_threads
    scans = []

    def visible(snapshot, visibility, **kwargs):
        scans.append(snapshot)
        return original(snapshot, visibility, **kwargs)

    monkeypatch.setattr(RegistrySnapshot, "visible_threads", visible)
    for name, members in (("#team", ("first", "second")), ("#both", ("first",)),
                          ("#all", ("first", "second"))):
        scans.clear()
        actions = CliCommand.target_catalog(comms, CliCommand.selected_targets(name), project=str(tmp_path))
        assert len(scans) == 1
        assert next(action.bound for action in actions if action.declaration is StartCliCommand) == tuple(
            StartCliCommand(name=member) for member in members)
        assert next(action.bound for action in actions if action.declaration is StopCliCommand) == tuple(
            StopCliCommand(name=member) for member in members)


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
