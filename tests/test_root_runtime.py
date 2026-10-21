"""Root attachment and publication use one persisted runtime owner."""

import pytest

from agent_comms.comms import Comms
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceOperator
from agent_comms.root_runtime import ForeignRuntimeError, RootRuntime, RootRuntimeStore


def test_root_runtime_attachment_family(tmp_path):
    owner, target, foreign = (
        LaunchRuntime(tmp_path / name / "bin/python")
        for name in ("owner", "target", "foreign")
    )
    root = tmp_path / "bus"
    RootRuntime.require_attachable(root, owner)
    store = RootRuntimeStore.at(root)
    assert store.read() == RootRuntime(owner.prefix)
    RootRuntime.require_attachable(root, owner)
    with pytest.raises(ForeignRuntimeError) as refused:
        RootRuntime.require_attachable(root, foreign)
    assert str(owner.prefix) in str(refused.value)
    assert str(foreign.prefix) in str(refused.value)

    barrier = MaintenanceBarrier(root / "registry.json")
    held = barrier.begin(MaintenanceOperator.current(), target=target)
    RootRuntime.require_attachable(root, target)
    RootRuntime.require_attachable(root, owner)
    with pytest.raises(ForeignRuntimeError):
        RootRuntime.require_attachable(root, foreign)
    barrier.clear(held)
    with pytest.raises(ForeignRuntimeError):
        RootRuntime.require_attachable(root, target)
    RootRuntime.publish(root, target)
    RootRuntime.require_attachable(root, target)
    with pytest.raises(ForeignRuntimeError):
        RootRuntime.require_attachable(root, owner)


def test_comms_refuses_before_opening_foreign_stores(tmp_path):
    root = tmp_path / "bus"
    other = LaunchRuntime(tmp_path / "other/bin/python")
    RootRuntime.publish(root, other)
    before = set(root.iterdir())
    with pytest.raises(ForeignRuntimeError):
        Comms(root)
    assert set(root.iterdir()) == before
