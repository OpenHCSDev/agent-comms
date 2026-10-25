"""Root attachment and publication use one persisted runtime owner."""


import os
import subprocess
from pathlib import Path

import agent_comms
import pytest

from agent_comms.comms import wire
from agent_comms.errors import RelationViolationError
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.locked_store import RegistryFile
from agent_comms.maintenance_barrier import (
    MaintenanceBarrier,
    MaintenanceOperator,
    NoMaintenanceTarget,
)
from agent_comms.root_runtime import ForeignRuntimeError, RootRuntime, RootRuntimeStore
from agent_comms.bus import Bus


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

    barrier = MaintenanceBarrier(RegistryFile.path_in(root))
    original = barrier.begin(MaintenanceOperator.current())
    assert not original.allows_runtime(target)
    with pytest.raises(ForeignRuntimeError):
        RootRuntime.require_attachable(root, target)
    held = barrier.retarget(original, target)
    assert held.generation == original.generation + 1
    with pytest.raises(RelationViolationError):
        barrier.retarget(original, foreign)
    assert not NoMaintenanceTarget().allows_runtime(foreign)
    RootRuntime.require_attachable(root, target)
    RootRuntime.require_attachable(root, owner)
    with pytest.raises(ForeignRuntimeError):
        RootRuntime.require_attachable(root, foreign)
    cleared = barrier.clear(held)
    assert cleared.target == NoMaintenanceTarget()
    with pytest.raises(ForeignRuntimeError):
        RootRuntime.require_attachable(root, target)
    RootRuntime.publish(root, target)
    RootRuntime.require_attachable(root, target)
    with pytest.raises(ForeignRuntimeError):
        RootRuntime.require_attachable(root, owner)


def test_recorded_runtime_launches_its_own_source_without_borrowing_a_runtime(tmp_path):
    runtime = RootRuntime(LaunchRuntime.running().prefix).runtime()
    completed = subprocess.run(
        (runtime.interpreter, "-c", "import agent_comms; print(agent_comms.__file__)"),
        cwd=tmp_path, env=runtime.environment(os.environ),
        capture_output=True, text=True, check=True,
    )
    assert Path(completed.stdout.strip()).resolve() == Path(agent_comms.__file__).resolve()
    foreign = LaunchRuntime.at_prefix(tmp_path / "foreign")
    assert "PYTHONPATH" not in foreign.environment({"PYTHONPATH": "foreign-source"})


def test_comms_creates_a_private_root_and_refuses_foreign_stores(tmp_path):
    created = wire(tmp_path / "created")
    assert RootRuntimeStore.at(created.root).read().prefix == LaunchRuntime.running().prefix
    assert Bus(Bus.path_in(created.root)).revision() == 0

    root = tmp_path / "bus"
    other = LaunchRuntime(tmp_path / "other/bin/python")
    RootRuntime.publish(root, other)
    with pytest.raises(ForeignRuntimeError):
        wire(root)
    assert not RegistryFile.path_in(root).exists()
    assert not Bus.path_in(root).exists()
