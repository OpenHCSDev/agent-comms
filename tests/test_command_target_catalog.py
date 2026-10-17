"""Target families never disguise a broken binding as an unavailable command."""
from agent_comms.storage_rules import StorageRule

import pytest

from agent_comms.channel_targets import ChannelTarget, ProjectTarget, ThreadTarget
from agent_comms.cli_commands import CliCommand, ReadTargetCliCommand
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.threads import Thread


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
            Thread.create(name, frozenset({"team"}), str(tmp_path), storage_rule=StorageRule()), StoppedThreadStatus(),
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
            CliCommand.target_catalog(comms, selection, project=str(tmp_path))
